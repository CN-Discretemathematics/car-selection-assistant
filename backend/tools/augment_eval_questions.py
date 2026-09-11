"""口语化改写增强（评测规范 v3/v6.2）：用 LLM 把模板题改写成真实用户口吻，量化词面泄漏。

模板题由生成器从真实数据构造，措辞与索引文本同源——BM25 的成绩含「见过原句」成分。
本工具对每条可评测问题生成 1 条口语化改写变体，输出仅含变体的问题库，供
eval_rag.py 以同一口径评测：原题 vs 改写体的指标差即词面泄漏的量化。

校验原则（v6.2 重构）：**在评测消费的语义结构层校验，不在表面形式层校验**——
变体有效当且仅当它在评测自身的判定机制下与原题同真值：
1. 数字语义等价（中文数字/万单位换算后多集一致）——约束数字漂移会被评测判为不同题；
2. 实体可解析：点名车系的题，改写体经生产解析器（resolve_series，含别名/款型名）
   仍必须解析到原锚定车系——解析失败的问题在评测中真值不可恢复，正确拒绝；
   同时产出「解析鲁棒率」这一独立产品指标（口语化表述下解析层的存活率）；
3. 约束结构保持：recommend/semantic 题的能源/车身约束必须能从改写体用与评测同源的
   措辞映射反解出来（expect.energy_type ↔ ENERGY_TO_HINT 等）——否则评测将用
   原题约束判定一道已经变意的问题（问题漂移污染词面泄漏度量）。

被拒绝的改写按原因计数输出（resolver/约束/数字/其他），拒绝不是浪费——resolver
拒绝率本身就是解析层在口语表述下的鲁棒性测量。

用法：
    python tools/augment_eval_questions.py --questions eval/questions.json \
        --output eval/questions-paraphrased.json --concurrency 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common.llm import LLMClient  # noqa: E402

_PROMPT = (
    "把下面的用户买车提问改写成更口语化的说法，像真实用户在聊天窗口随手打字。\n"
    "硬性要求：\n"
    "1. 数字语义必须与原句完全一致（预算/座位/续航不得增减），可以用中文数字（如十五万）；\n"
    "2. 以下词必须原样保留、一个字都不能改：{protected}；\n"
    "3. 不新增任何要求、车型或配置；不改变原意；\n"
    "4. 只输出改写后的一句话，不要任何解释或引号。\n"
    "改写示例：\n"
    "  原句：预算15万，想要一台纯电SUV，家里5口人用，有推荐吗\n"
    "  改写：家里五口人，15万以内想买个纯电的SUV，有啥推荐的\n"
    "用户提问：{text}"
)

# 中文数字等价判定（v4 扩样）：「十五万」≡ 150000、「六座」≡ 6座——
# 首版要求阿拉伯数字逐组一致，28% 改写成功率过低（LLM 爱用中文数字）。
_CN_NUM_RE = re.compile(r"[零一二两三四五六七八九十百千]+(?:万)?")
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(cn: str) -> int | None:
    has_wan = cn.endswith("万")
    body = cn[:-1] if has_wan else cn
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    num = 0
    any_digit = False
    for ch in body:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
            any_digit = True
        elif ch in units:
            total += (num or 1) * units[ch]
            num = 0
            any_digit = True
        else:
            return None
    total += num
    if not any_digit:
        return None
    return total * (10000 if has_wan else 1)


def _number_semantics(text: str) -> list[int]:
    """把「15万」「十五万」统一成语义整数，返回排序后的数字多集（口径：语义等价）。"""
    text = re.sub(r"(\d+(?:\.\d+)?)\s*万", lambda m: str(int(float(m.group(1)) * 10000)), text)
    parts: list[str] = []
    pos = 0
    for m in _CN_NUM_RE.finditer(text):
        parts.append(text[pos:m.start()])
        value = _cn_to_int(m.group())
        parts.append(str(value) if value is not None else m.group())
        pos = m.end()
    parts.append(text[pos:])
    return sorted(int(x) for x in re.findall(r"\d+", "".join(parts)))


def _validate(
    original: str,
    variant: str,
    q: dict,
    resolve_ok: bool,
) -> tuple[bool, str]:
    """语义结构校验：返回 (是否有效, 拒绝原因)。

    与旧版的差异：车系名逐字保留 → 实体可解析（生产解析器判定）；
    新增约束结构保持（能源/车身约束必须能从改写体用评测同源映射反解）。
    """
    if not variant or not (6 <= len(variant) <= 120):
        return False, "length"
    if variant == original:
        return False, "identical"
    if _number_semantics(variant) != _number_semantics(original):
        return False, "digits"
    anchors = q.get("anchors") or {}
    expect = q.get("expect") or {}
    if anchors.get("series_id") and not resolve_ok:
        return False, "resolver"
    # 约束结构保持（与评测同源的措辞映射；v6.2 新增——防止改写丢约束后被原题约束误判）
    from tools.gen_eval_questions import ENERGY_TO_HINT, HEAD_LABEL_TO_BODY

    if expect.get("energy_type"):
        hint = (ENERGY_TO_HINT.get(expect["energy_type"]) or "").rstrip("的")
        if hint and hint not in variant:
            return False, "constraint"
    if expect.get("body_type"):
        label = HEAD_LABEL_TO_BODY.get(expect["body_type"]) or ""
        if label and label not in variant:
            return False, "constraint"
    return True, ""


async def _rewrite(
    llm: LLMClient,
    sem: asyncio.Semaphore,
    q: dict,
    out: list,
    series_names: dict,
    resolve_series,
    db,
    rejects: dict,
) -> None:
    async with sem:
        sid = (q.get("anchors") or {}).get("series_id")
        series_name = series_names.get(int(sid)) if sid else None
        brand_name = (q.get("anchors") or {}).get("brand_name") or ""
        protected = "、".join(n for n in (brand_name, series_name) if n) or "（本题无固定车系名）"
        try:
            resp = await llm.chat(
                [{"role": "user", "content": _PROMPT.format(text=q["text"], protected=protected)}],
                temperature=0.9,
            )
            text = str(resp["choices"][0]["message"]["content"] or "").strip().strip("\"“”")
        except Exception:  # noqa: BLE001 - 单条失败直接跳过（改写体不足不影响其余）
            return
        resolve_ok = False
        if (q.get("anchors") or {}).get("series_id"):
            resolved_ids = {s.id for s, _ in resolve_series(db, text)}
            resolve_ok = int(q["anchors"]["series_id"]) in resolved_ids
        ok, reason = _validate(q["text"], text, q, resolve_ok)
        if not ok:
            rejects[reason] = rejects.get(reason, 0) + 1
            return
        out.append({**q, "id": f"v{q['id'][1:]}", "parent_id": q["id"], "text": text, "paraphrased": True})


async def _run(args, questions: list[dict], series_names: dict) -> tuple[list[dict], dict]:
    llm = LLMClient()
    if not llm.available:
        raise RuntimeError("LLM 未配置：改写增强需要 DEEPSEEK_API_KEY")
    from app.catalog.series_index import resolve_series

    from app.common.database import get_session_factory

    factory = get_session_factory()
    sem = asyncio.Semaphore(args.concurrency)
    out: list[dict] = []
    rejects: dict = {}
    with factory() as db:
        tasks = [
            asyncio.create_task(
                _rewrite(llm, sem, q, out, series_names, resolve_series, db, rejects)
            )
            for q in questions
            if q.get("bucket") != "unanswerable"  # 不可回答题走答案层拒答评测，不改写
        ]
        done = 0
        for chunk in asyncio.as_completed(tasks):
            await chunk
            done += 1
            if done % 100 == 0:
                print(f"  改写进度 {done}/{len(tasks)}（成功 {len(out)}）", flush=True)
        await asyncio.gather(*tasks, return_exceptions=True)
    return out, rejects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="口语化改写增强（LLM 生成变体，锚点/数字不变）")
    parser.add_argument("--questions", default=os.path.join("eval", "questions.json"))
    parser.add_argument("--output", default=os.path.join("eval", "questions-paraphrased.json"))
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args(argv)

    with open(args.questions, encoding="utf-8") as fh:
        questions = json.load(fh)["questions"]

    from sqlalchemy import select

    from app.common.database import get_session_factory
    from app.common.models import VehicleSeries

    with get_session_factory()() as db:
        series_names = {s.id: s.name for s in db.scalars(select(VehicleSeries)).all()}

    out, rejects = asyncio.run(_run(args, questions, series_names))
    anchored = sum(1 for q in questions if (q.get("anchors") or {}).get("series_id"))
    resolver_pass = anchored - rejects.get("resolver", 0)
    print(f"改写变体 {len(out)} 条（原题 {len(questions)} 条）→ {args.output}")
    print(
        f"拒绝原因：{json.dumps(rejects, ensure_ascii=False)}；"
        f"解析鲁棒率 {resolver_pass}/{anchored}"
        f"（{resolver_pass / anchored:.1%}）" if anchored else "解析鲁棒率 n/a"
    )
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    payload = {
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "口语化改写变体（LLM 改写，锚点/约束/数字经硬校验不变）",
        "count": len(out),
        "questions": out,
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"改写变体 {len(out)} 条（原题 {len(questions)} 条）→ {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
