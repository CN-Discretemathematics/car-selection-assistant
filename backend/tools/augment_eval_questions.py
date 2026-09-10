"""口语化改写增强（评测规范 v3）：用 LLM 把模板题改写成真实用户口吻，量化词面泄漏。

模板题由生成器从真实数据构造，措辞与索引文本同源——BM25 的成绩含「见过原句」成分。
本工具对每条可评测问题生成 1 条口语化改写变体（锚点/约束/数字不变），输出仅含变体的
问题库，供 eval_rag.py 以同一口径评测：原题 vs 改写体的指标差即词面泄漏的量化。

校验（硬性，不过即丢弃该变体）：
- 原句中的全部数字串必须原样保留（预算/座位/续航数字，防 LLM 改数字或换中文数字）；
- 原句点名车系/款型时，车系名不得丢失（防实体解析失效造成假跌）；
- 长度 6~120 字符。

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
    "1. 原句中的所有数字必须原样保留，用阿拉伯数字，不得增减或改成中文数字；\n"
    "2. 品牌名、车系名、款型名必须原样保留，一个字都不能改；\n"
    "3. 不新增任何要求、车型或配置；不改变原意；\n"
    "4. 只输出改写后的一句话，不要任何解释或引号。\n"
    "用户提问：{text}"
)


def _digits(text: str) -> list[str]:
    return sorted(re.findall(r"\d+", text))


def _validate(original: str, variant: str, series_name: str | None) -> bool:
    if not variant or not (6 <= len(variant) <= 120):
        return False
    if _digits(variant) != _digits(original):
        return False  # 数字必须逐组一致（预算/座位/续航不得改动）
    if series_name and series_name not in variant:
        return False  # 点名车系的题：车系名丢了会让实体解析失效（假跌）
    return variant != original


async def _rewrite(llm: LLMClient, sem: asyncio.Semaphore, q: dict, out: list, series_names: dict) -> None:
    async with sem:
        try:
            resp = await llm.chat(
                [{"role": "user", "content": _PROMPT.format(text=q["text"])}],
                temperature=0.9,
            )
            text = str(resp["choices"][0]["message"]["content"] or "").strip().strip("\"“”")
        except Exception:  # noqa: BLE001 - 单条失败直接跳过（改写体不足不影响其余）
            return
        sid = (q.get("anchors") or {}).get("series_id")
        series_name = series_names.get(int(sid)) if sid else None
        if not _validate(q["text"], text, series_name):
            return
        out.append({**q, "id": f"v{q['id'][1:]}", "parent_id": q["id"], "text": text, "paraphrased": True})


async def _run(args, questions: list[dict], series_names: dict) -> list[dict]:
    llm = LLMClient()
    if not llm.available:
        raise RuntimeError("LLM 未配置：改写增强需要 DEEPSEEK_API_KEY")
    sem = asyncio.Semaphore(args.concurrency)
    out: list[dict] = []
    tasks = [
        asyncio.create_task(_rewrite(llm, sem, q, out, series_names))
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
    return out


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

    out = asyncio.run(_run(args, questions, series_names))
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
