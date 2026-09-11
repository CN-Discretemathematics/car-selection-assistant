"""答案层 LLM-as-judge 评测（评测规范 v5）：faithfulness / completeness / 拒答正确性。

与作答模型分离：作答走 AgentEngine（DeepSeek，生产同款路径），
judge 走独立模型（默认 DashScope qwen-plus，可用 JUDGE_MODEL/--judge-model 覆盖）。

流程（分层抽样 N 题，每题独立会话 + 240s 硬超时）：
1. 作答：AgentEngine（含 LLM）按生产路径生成回答；
2. 证据：rag.search 取回 top-5 证据（与回答同一检索口径）；
3. 判定：judge 按维度打分——
   - faithfulness：回答的事实性主张是否全部被证据支持（幻觉检测）；
   - completeness：问题问到的参数/诉求是否被回答覆盖（期望要点来自生成器结构化字段）；
   - refusal：不可回答题是否显式「官方资料未披露」（确定性字符串判定）；
   - faithful_db（确定性，零 judge 成本）：回答中的数字必须能追溯到锚定车系的
     DB 数据（事实值/单位/指导价元与万元/款型名/在售数）或问题本身，否则视为编造；
4. 双评一致性：默认 30% 的题由 judge 二次独立评分，输出一致率（校准 judge 可靠性）。

用法：
    python tools/eval_judge.py --questions eval/questions.json --sample 100 \
        --report eval/eval-judge.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.agent.engine import AgentEngine  # noqa: E402
from app.agent.session import SessionStore  # noqa: E402
from app.common.database import get_session_factory  # noqa: E402
from app.common.llm import LLMClient  # noqa: E402
from app.common.models import MonthlySales, OfficialPrice, SpecFact, VehicleVariant  # noqa: E402
from app.rag import service as rag  # noqa: E402

_UNANSWERABLE_MARKERS = ("未披露", "未查到", "暂无", "没有披露", "没有查到", "没有公布")
# 评审 R4#10：保留「没有」的诚实复合短语，仅剔除裸「没有」（会误放行编造回答）  # 评审 E9：裸「没有」误放行编造回答
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

FAITHFULNESS_SYSTEM = (
    "你是严格的 RAG 事实性审核员。给定【回答】与【检索证据】，判断回答中的事实性主张"
    "是否全部被证据直接支持。证据不足以支持的主张一律算不支持。"
    '只输出 JSON：{"faithful": true|false, "unsupported_claims": ["..."]}'
)
COMPLETENESS_SYSTEM = (
    "你是严格的 RAG 完整性审核员。给定【问题】【期望要点】【回答】，判断回答是否覆盖了"
    "期望要点（有数据给值，无数据须明确说未披露）。"
    '只输出 JSON：{"complete": true|false, "missing": ["..."]}'
)


def _numbers(text: str) -> set[float]:
    """提取文本中的数字为 float 集合（去除千分位逗号；浮点语义比较，防 '17.60'≠'17.6'）。"""
    return {float(x) for x in _NUM_RE.findall((text or "").replace(",", ""))}


def _db_number_pool(db, series_ids: set[int]) -> set[float]:
    """相关车系的「可出现在回答里的数字」池（确定性 faithfulness 判定的基准）。

    来源：在售款型展示名/事实值/单位（键名含数字如 L/100km 的 100 也入池）、
    官方指导价（元与万元两种形态）、月销量与月份、在售款型数——回答里的任何数字
    都应能追溯到其中之一，否则视为编造。
    """
    pool: set[float] = set()
    variants = db.scalars(
        select(VehicleVariant).where(
            VehicleVariant.status == "on_sale", VehicleVariant.series_id.in_(series_ids)
        )
    ).all()
    vids = [v.id for v in variants]
    for v in variants:
        pool.update(_numbers(v.display_name or ""))
    if vids:
        for key, value, unit in db.execute(
            select(SpecFact.fact_key, SpecFact.fact_value, SpecFact.unit)
            .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
            .where(VehicleVariant.status == "on_sale", VehicleVariant.series_id.in_(series_ids))
        ).all():
            # 事实键也入池：键名含数字（如「WLTC综合油耗(L/100km)」的 100），
            # 回答按「键 = 值」引用时会带上键名 token（2026-09-11 假阳性复核）
            pool.update(_numbers(key or ""))
            pool.update(_numbers(value or ""))
            pool.update(_numbers(unit or ""))
        for p in db.scalars(
            select(OfficialPrice).where(
                OfficialPrice.effective_to.is_(None), OfficialPrice.variant_id.in_(vids)
            )
        ).all():
            price = float(p.price_cny)
            pool.update(_numbers(f"{price:g}"))
            pool.update(_numbers(f"{price / 10000:g}"))
        pool.update(_numbers(str(len(variants))))
        # 月销量与月份也在回答中出现（车系画像切片的销量句），入池
        for month, count in db.execute(
            select(MonthlySales.month, MonthlySales.sales_count)
            .join(VehicleVariant, MonthlySales.series_id == VehicleVariant.series_id)
            .where(
                VehicleVariant.status == "on_sale",
                VehicleVariant.series_id.in_(series_ids),
                MonthlySales.sales_type.in_(("retail", "portal")),
            )
            .distinct()
        ).all():
            pool.update(_numbers(month))
            if count:
                pool.update(_numbers(str(count)))
    return pool


def _parse_judge_json(text: str) -> dict | None:
    """解析 judge 的 JSON 输出（容忍 ```json 围栏与前后缀）。"""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class JudgeClient:
    """独立 judge 模型客户端（与作答模型分离；失败返回 None 由调用方降级）。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._base = base_url.rstrip("/")
        if not self._base.endswith("/v1"):
            self._base += "/v1"
        self._api_key = api_key
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def chat_json(self, system: str, user: str, temperature: float = 0.0) -> dict | None:
        import httpx

        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self._base}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return _parse_judge_json(content)


def _stratified_sample(questions: list[dict], sample: int, rng: random.Random) -> list[dict]:
    """按桶分层抽样（保证每桶都有代表；unanswerable 单独成库时全量取用）。"""
    if sample <= 0 or len(questions) <= sample:
        return questions
    by_bucket: dict[str, list[dict]] = {}
    for q in questions:
        by_bucket.setdefault(q.get("bucket") or "uncategorized", []).append(q)
    per = max(1, sample // max(len(by_bucket), 1))
    out: list[dict] = []
    for bucket in sorted(by_bucket):
        pool = by_bucket[bucket][:]
        rng.shuffle(pool)
        out.extend(pool[:per])
    i = 0
    while len(out) < sample:
        out.append(questions[i % len(questions)])
        i += 1
    return out[:sample]


def _summarize(rows: list[dict]) -> dict:
    n = len(rows)
    faith = [r["faithful"] for r in rows if r.get("faithful") is not None]
    comp = [r["complete"] for r in rows if r.get("complete") is not None]
    refu = [r["refusal_ok"] for r in rows if r.get("refusal_ok") is not None]
    fdb = [r["faithful_db"] for r in rows if r.get("faithful_db") is not None]
    double = [r for r in rows if r.get("faithful_second") is not None]
    agree = [1.0 for r in double if r.get("faithful") == r.get("faithful_second")]
    errors = [r for r in rows if r.get("error")]
    return {
        "questions": n,
        "errors": len(errors),  # 评审 E11：错误/超时行单独计数，不混入样本量
        "faithful_rate": round(sum(faith) / len(faith), 4) if faith else None,
        "faithful_judged": len(faith),
        "faithful_db_rate": round(sum(fdb) / len(fdb), 4) if fdb else None,
        "faithful_db_judged": len(fdb),
        "complete_rate": round(sum(comp) / len(comp), 4) if comp else None,
        "complete_judged": len(comp),
        "refusal_ok_rate": round(sum(refu) / len(refu), 4) if refu else None,
        "double_judge_agreement": round(sum(agree) / len(double), 4) if double else None,
        "double_judge_n": len(double),
    }


async def _run_one(
    q: dict,
    judge: JudgeClient,
    factory,
    engine: AgentEngine,
    variant_series: dict[int, int],
    sem: asyncio.Semaphore,
    double_judge: bool,
    out: list[dict],
) -> None:
    """单题执行 + 240s 硬超时兜底：任何一题卡死不再拖垮整体评测。

    已知限制（评审 E3）：超时取消可能落在 engine 内部线程池查询进行中——任务会话
    的 close 与孤儿线程收尾存在竞争；错误被线程吞掉、会话按题独立不影响其他任务，
    但该题连接可能异常回收。生产超时极少触发，暂不为评测工具引入线程级会话隔离。
    """
    async with sem:
        try:
            await asyncio.wait_for(
                _run_one_inner(q, judge, factory, engine, variant_series, double_judge, out),
                timeout=240,
            )
        except asyncio.TimeoutError:
            out.append({"id": q.get("id"), "bucket": q.get("bucket"),
                        "question": (q.get("text") or "")[:120], "error": "timeout 240s"})
        except Exception as err:  # noqa: BLE001 - 单题失败不中断整体
            out.append({**{"id": q.get("id"), "bucket": q.get("bucket")},
                        "error": f"{type(err).__name__}: {str(err)[:160]}"})


async def _run_one_inner(
    q: dict,
    judge: JudgeClient,
    factory,
    engine: AgentEngine,
    variant_series: dict[int, int],
    double_judge: bool,
    out: list[dict],
) -> None:
    text = q.get("text") or ""
    expect = q.get("expect") or {}
    row: dict = {"id": q.get("id"), "bucket": q.get("bucket"), "question": text[:120]}
    with factory() as db:
        store = engine._store  # noqa: SLF001 - 评测需要独立会话驱动同一引擎
        out_obj = await engine.handle(db, store.create(), text)
        answer = getattr(out_obj, "explanation", "") or ""
        # 评审 E12 说明：evidence 为按原问题复检索的 top-5，不是 agent 实际引用的
        # 全部来源——LLM faithful 度量「检索证据对回答的支撑率」；DB 直答类回答
        # （参数/对比模板）的忠实度以 faithful_db（对照锚定车系全量 DB 数字池）为准
        evidence = rag.search(db, text, top_k=5)
        # 确定性 faithfulness（对照 DB 事实池，零 judge 成本）
        rel_series = set()
        a = q.get("anchors") or {}
        if a.get("series_id"):
            rel_series.add(int(a["series_id"]))
        for vid in a.get("variant_ids") or []:
            sid = variant_series.get(int(vid))
            if sid:
                rel_series.add(int(sid))
        if a.get("variant_id"):
            sid = variant_series.get(int(a["variant_id"]))
            if sid:
                rel_series.add(int(sid))
        if rel_series:
            pool = _db_number_pool(db, rel_series)
            unsupported = _numbers(answer) - _numbers(text) - pool
            row["faithful_db"] = not unsupported
            if unsupported:
                row["unsupported_numbers"] = sorted(unsupported)[:8]
    row["answer_excerpt"] = answer[:160]
    ev_text = "\n---\n".join(h.text for h in evidence) or "（无证据）"
    # faithfulness（judge）
    try:
        j1 = await judge.chat_json(
            FAITHFULNESS_SYSTEM,
            f"【回答】：\n{answer}\n\n【检索证据】：\n{ev_text}",
        )
        row["faithful"] = j1.get("faithful") if j1 else None
        row["unsupported_claims"] = (j1 or {}).get("unsupported_claims") or []
    except Exception as err:  # noqa: BLE001
        row["faithful"] = None
        row["unsupported_claims"] = [f"judge-error: {type(err).__name__}"]
    # completeness（judge；期望要点来自生成器结构化字段）
    wanted: list[str] = []
    if expect.get("fact_key"):
        wanted.append(f"参数 {expect['fact_key']}")
    if expect.get("body_type"):
        wanted.append(f"车身形式 {expect['body_type']}")
    if expect.get("energy_type"):
        wanted.append(f"能源类型 {expect['energy_type']}")
    if expect.get("unanswerable"):
        wanted.append("明确说明该数据未披露")
    if wanted:
        try:
            j2 = await judge.chat_json(
                COMPLETENESS_SYSTEM,
                f"【问题】：{text}\n【期望要点】：{'；'.join(wanted)}\n【回答】：\n{answer}",
            )
            row["complete"] = j2.get("complete") if j2 else None
            row["missing"] = (j2 or {}).get("missing") or []
        except Exception as err:  # noqa: BLE001
            row["complete"] = None
            row["missing"] = [f"judge-error: {type(err).__name__}"]
    # 拒答诚实性（确定性字符串判定优先）
    if expect.get("unanswerable"):
        row["refusal_ok"] = any(marker in answer for marker in _UNANSWERABLE_MARKERS)
    # 双评一致性抽样（评审 E7）：子集由 _drive 预先按种子确定；两评均 temperature=0，
    # 一致率度量 judge 稳定性而非采样噪声
    if double_judge and row.get("faithful") is not None:
        try:
            j3 = await judge.chat_json(
                FAITHFULNESS_SYSTEM,
                f"【回答】：\n{answer}\n\n【检索证据】：\n{ev_text}",
            )
            row["faithful_second"] = j3.get("faithful") if j3 else None
        except Exception as err:  # noqa: BLE001 - 双评失败不影响主指标
            row["faithful_second"] = None
    out.append(row)


def _build_judge(args) -> JudgeClient:
    base = args.judge_base_url or os.environ.get("EMBEDDING_BASE_URL", "")
    key = args.judge_api_key or os.environ.get("EMBEDDING_API_KEY", "")
    model = args.judge_model or os.environ.get("JUDGE_MODEL", "qwen-plus")
    return JudgeClient(base, key, model)


async def _drive(args, questions: list[dict], judge: JudgeClient, rows: list[dict]) -> None:
    factory = get_session_factory()
    engine = AgentEngine(llm=LLMClient(), store=SessionStore())
    sem = asyncio.Semaphore(max(1, args.concurrency))  # 评审 E14：0 会使所有任务永久阻塞
    # 评审 E7：双评子集在调度前按种子确定性选定（并发下共享 rng 的抽样顺序不可复现）
    rng = random.Random(20260911)
    double_ids = {
        q.get("id") for q in questions if rng.random() < args.double_rate
    }
    with factory() as db:
        variant_series = {
            v.id: v.series_id
            for v in db.scalars(
                select(VehicleVariant).where(VehicleVariant.status == "on_sale")
            ).all()
        }
    tasks = [
        asyncio.create_task(
            _run_one(q, judge, factory, engine, variant_series, sem,
                     q.get("id") in double_ids, rows)
        )
        for q in questions
    ]
    done = 0
    for fut in asyncio.as_completed(tasks):
        try:
            await fut
        except Exception as err:  # noqa: BLE001 - 单题异常不中断整体评测
            rows.append({"error": f"{type(err).__name__}: {str(err)[:160]}"})
        done += 1
        if done % 10 == 0:
            print(f"  judge 进度 {done}/{len(tasks)}（完成 {len(rows)}）", flush=True)
    await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="答案层 LLM-as-judge 评测（faithfulness/completeness/refusal）")
    parser.add_argument("--questions", default=os.path.join("eval", "questions.json"))
    parser.add_argument("--sample", type=int, default=100, help="分层抽样题数（0=全部）")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--double-rate", type=float, default=0.3, help="双评一致性抽样比例")
    parser.add_argument("--judge-base-url", default="", help="judge 端点（默认取 EMBEDDING_BASE_URL）")
    parser.add_argument("--judge-api-key", default="", help="judge 密钥（默认取 EMBEDDING_API_KEY）")
    parser.add_argument("--judge-model", default="", help="judge 模型（默认 qwen-plus，可用 JUDGE_MODEL 覆盖）")
    parser.add_argument("--report", default=os.path.join("eval", "eval-judge.json"))
    args = parser.parse_args(argv)

    if not os.path.exists(args.questions):  # 评审 E10：缺失文件友好报错
        print(f"问题库不存在：{args.questions}（先用 tools/gen_eval_questions.py 生成）", file=sys.stderr)
        return 2
    with open(args.questions, encoding="utf-8") as fh:
        questions = json.load(fh)["questions"]
    questions = [q for q in questions if (q.get("text") or "").strip()]
    sample = _stratified_sample(questions, args.sample, random.Random(20260911))

    judge = _build_judge(args)
    # rows 由 _drive 增量填充：中途崩溃/超时也保留已完成部分（报告降级输出）。
    # 评审 E15：捕获 BaseException（含 Ctrl-C），写完报告后再向上传播中断信号
    rows: list[dict] = []
    interrupted: BaseException | None = None
    try:
        asyncio.run(_drive(args, sample, judge, rows))
    except BaseException as err:  # noqa: BLE001 - 部分结果仍写入报告
        interrupted = err
        print(f"评测中断（保留部分结果）：{type(err).__name__}: {str(err)[:200]}", file=sys.stderr)

    summary = _summarize(rows)
    by_bucket = {
        bucket: _summarize([r for r in rows if (r.get("bucket") or "uncategorized") == bucket])
        for bucket in sorted({r.get("bucket") or "uncategorized" for r in rows})
    }
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "judge_model": judge.model,
        "sample": len(rows),
        "summary": summary,
        "by_bucket": by_bucket,
        "rows": rows,
    }
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"summary": summary, "by_bucket": by_bucket}, ensure_ascii=False, indent=1))
    print(f"报告：{args.report}")
    if interrupted is not None:
        if isinstance(interrupted, KeyboardInterrupt):
            return 130
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
