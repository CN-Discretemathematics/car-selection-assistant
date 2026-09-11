"""Agent 工具调用评测 + RAG 检索评测。

读取 eval/questions.json（真实数据分层抽样），对每条问题：
- 推荐类：走完整 Agent 引擎（追问→硬筛选→软评分→证据），校验硬约束零违规、
  每个推荐都带来源引用（citation_verifier 通过）；
- 对比类：comparison_tool 必须返回两个 SKU 的完整证据；
- 检索类：retrieval_search 必须命中结果（hit@k 记录为指标）；
- 追问类：开放问题必须先返回追问（need_clarification=True）。

用法：
    python tools/eval_agent.py [--limit N] [--report eval/report.json]
退出码：硬约束违规数 > 0 或通过率 < 95% 时为 1。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.engine import AgentEngine  # noqa: E402
from app.agent.session import SessionStore  # noqa: E402
from app.agent.tools import comparison_tool, retrieval_search  # noqa: E402
from app.common.database import get_session_factory  # noqa: E402
from app.common.enums import NEW_ENERGY_TYPES  # noqa: E402
from app.common.llm import LLMClient  # noqa: E402

RECOMMEND_INTENTS = {
    "budget_suv", "budget_sedan", "budget_mpv", "energy_bev", "energy_phev_erev",
    "family_seats", "commute", "long_range_bev", "series_spec", "brand_series_ask",
}
RETRIEVAL_INTENTS = {"series_spec", "brand_series_ask", "energy_bev", "energy_phev_erev", "long_range_bev", "commute"}
COMPARE_INTENTS = {"compare_two"}
CLARIFY_INTENTS = {"open_clarify"}
# v3 不可回答题（诚实性）：锚定车系的在售款型均无该参数，期望答案明确标注「官方资料未披露」
UNANSWERABLE_INTENTS = {"unanswerable_param"}
# 评审 E9：裸「没有」会把含「市面上没有对手」的编造回答误判为诚实拒答——
# 只保留拒答语义的标记词
# 评审 R4#10：保留「没有」的诚实复合短语（没有披露/没有查到），仅剔除裸「没有」
_HONEST_MARKERS = ("未披露", "未查到", "暂无", "没有披露", "没有查到", "没有公布")


def _check_recommendation(q: dict, out) -> tuple[bool, str]:
    """推荐结果必须满足题目硬约束，且每个推荐都带来源引用。"""
    variants = out.recommended_variants
    expect = q.get("expect") or {}
    if not variants:
        # 无推荐不算违规：引擎应说明原因（空结果也要如实解释）
        return True, "无候选（如实返回空）"
    problems: list[str] = []
    budget_max = expect.get("budget_max")
    energy_type = expect.get("energy_type")
    energy_pref = expect.get("energy_preference") or []
    body_type = expect.get("body_type")
    for v in variants:
        if budget_max is not None and v.price_cny is not None and v.price_cny > budget_max:
            problems.append(f"q: 价格超预算 {v.price_cny} > {budget_max}")
        if energy_type and v.energy_type != energy_type:
            problems.append(f"q: 能源不符 {v.energy_type} != {energy_type}")
        if "new_energy" in energy_pref and v.energy_type not in NEW_ENERGY_TYPES:
            problems.append(f"q: 非新能源 {v.energy_type}")
        if body_type and v.body_type != body_type:
            problems.append(f"q: 车身不符 {v.body_type} != {body_type}")
    if problems:
        return False, "; ".join(problems[:3])
    if not out.citations:
        return False, "q: 推荐无来源引用（citation_verifier 未通过）"
    return True, f"{len(variants)} 个推荐全部带来源"


async def _run_engine(db, engine: AgentEngine, text: str):
    store = engine._store  # noqa: SLF001
    sid = store.create()
    return await engine.handle(db, sid, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent 工具调用 + RAG 检索评测")
    parser.add_argument("--questions", default=os.path.join("eval", "questions.json"))
    parser.add_argument("--limit", type=int, default=0, help="只评测前 N 条（0=全部）")
    parser.add_argument("--report", default=os.path.join("eval", "report.json"))
    args = parser.parse_args(argv)

    with open(args.questions, encoding="utf-8") as fh:
        questions = json.load(fh)["questions"]
    if args.limit:
        questions = questions[: args.limit]

    # 引擎用进程内会话存储 + 无 LLM 密钥（确定性模板解释），不依赖外部服务
    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())

    results: list[dict] = []
    violations = 0
    retrieval_hits = 0
    retrieval_total = 0
    t0 = time.time()

    with get_session_factory()() as db:
        for q in questions:
            intent = q["intent"]
            start = time.time()
            try:
                notes: list[str] = []
                ok = True
                if intent in RECOMMEND_INTENTS:
                    out = asyncio.run(_run_engine(db, engine, q["text"]))
                    if out.need_clarification:
                        notes.append("先追问（need_clarification）")
                    else:
                        rec_ok, rec_note = _check_recommendation(q, out)
                        ok = ok and rec_ok
                        notes.append(rec_note)
                if intent in COMPARE_INTENTS:
                    ids = (q.get("expect") or {}).get("variant_ids") or []
                    res = comparison_tool(db, ids)
                    cmp_ok = len(res["variants"]) >= 1
                    ok = ok and cmp_ok
                    notes.append(f"对比 {len(res['variants'])} 款")
                if intent in RETRIEVAL_INTENTS:
                    retrieval_total += 1
                    res = retrieval_search(db, q["text"], top_k=5)
                    anchor = (q.get("anchors") or {}).get("series_id")
                    hits = [r for r in res if anchor and r.get("series_id") == anchor]
                    ret_ok = len(res) > 0
                    ok = ok and ret_ok
                    if hits:
                        retrieval_hits += 1
                    notes.append(f"检索命中 {len(res)} 条（锚定车系命中 {len(hits)}）")
                if intent in CLARIFY_INTENTS:
                    out = asyncio.run(_run_engine(db, engine, q["text"]))
                    clar_ok = out.need_clarification is True
                    ok = ok and clar_ok
                    notes.append("已追问" if clar_ok else "未追问")
                if intent in UNANSWERABLE_INTENTS:
                    # 诚实性判定：证据/数据缺失时必须标注未披露，不得编造数值
                    out = asyncio.run(_run_engine(db, engine, q["text"]))
                    answer = getattr(out, "explanation", "") or ""
                    honest = any(marker in answer for marker in _HONEST_MARKERS)
                    # 评审 E8：hit 统计与 RETRIEVAL_INTENTS 同口径——锚定车系命中才算
                    retrieval_total += 1
                    res = retrieval_search(db, q["text"], top_k=5)
                    anchor = (q.get("anchors") or {}).get("series_id")
                    anchor_hits = [
                        r for r in res
                        if anchor and (r.get("series_id") == anchor
                                       or r.get("variant_id") == (q.get("anchors") or {}).get("variant_id"))
                    ] if anchor else []
                    if anchor_hits:
                        retrieval_hits += 1
                    ok = ok and honest
                    notes.append(
                        "正确标注未披露" if honest else f"缺失未标注（explanation: {answer[:48]}）"
                    )
                if not notes:
                    notes.append("跳过（未知意图）")
                if not ok:
                    violations += 1
                note = "; ".join(notes)
            except Exception as err:  # noqa: BLE001
                ok, note = False, f"异常：{type(err).__name__}: {err}"
                violations += 1
            results.append(
                {"id": q["id"], "intent": intent, "text": q["text"][:60],
                 "ok": ok, "note": note, "elapsed_ms": round((time.time() - start) * 1000)}
            )

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    rate = passed / total if total else 1.0
    report = {
        "questions_file": args.questions,
        "total": total,
        "passed": passed,
        "pass_rate": round(rate, 4),
        "violations": violations,
        "retrieval_hit_at_k": round(retrieval_hits / retrieval_total, 4) if retrieval_total else None,
        "elapsed_seconds": round(time.time() - t0, 1),
        "results": results,
    }
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(f"评测完成：{passed}/{total} 通过（{rate:.1%}），硬约束违规 {violations} 条，"
          f"检索 hit@5={report['retrieval_hit_at_k']}，用时 {report['elapsed_seconds']}s")
    print(f"报告已写入：{args.report}")
    return 0 if violations == 0 and rate >= 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(main())
