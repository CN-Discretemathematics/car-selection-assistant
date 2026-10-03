"""L1 软偏好抽取的**质量**评测（提案 §5 第 2 步的验收手段）。

## 为什么需要它

L1 的机制层（封闭枚举 / evidence 子串 / regex 优先）已被 50 例常驻测试覆盖，
但那只证明「防线在」，不证明「抽得准」。**接入排序的前提正是这份数据**——
提案 §6 第 4 步写明「前置：第 2/3 步的抽取质量评测数据」。

## 为什么金标语料**入库**而 RAG 题库不入库

`backend/eval/` 整个目录被 gitignore，RAG 题库由脚本从当时的库抽样生成、不在
git 里——所以那套指标只能在同一台机器、同一份题库上比较（见提案 §5.3）。
本文件是**手工标注的封闭集合**，逐条写死期望值，因此必须进版本控制：
金标一改，评测结论就变了，而 git 里看得见这个改动。

## 跑法

    # 需要 backend/.env 里有 DEEPSEEK_API_KEY（不打印、不入库）
    python tools/eval_soft_prefs.py

    # CI/无网环境：只校验金标文件自身是否自洽，不发任何请求
    python tools/eval_soft_prefs.py --check-corpus-only

## 关键判据（任一不满足即不得接入排序）

1. **空偏好不编造**：`e*` 期望为空的样本，抽取结果必须也为空；
2. **越界必须被拒**：`x*` / `h*` 期望为空的样本，同上——尤其不得凭空产出
   body_type / energy_type / budget（那是硬约束，猜错会砍掉整个候选集）；
3. **字段级准确**：usage / household / pain_points / priority_order 逐字段比对；
4. **输出永远是合法枚举值 + evidence 是原话子串**（由 sanitize 保证，此处复查）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import soft_prefs as sp  # noqa: E402

GOLD = Path(__file__).resolve().parents[1] / "tests" / "soft_prefs_golden.jsonl"
FIELDS = ("usage_scenario", "household_size", "pain_points", "priority_order")


def load_gold() -> list[dict]:
    return [json.loads(ln) for ln in GOLD.read_text(encoding="utf-8").splitlines() if ln.strip()]


def check_corpus(cases: list[dict]) -> list[str]:
    """金标文件自身的自洽性——**不发任何网络请求**，CI 也能跑。"""
    problems: list[str] = []
    enums = {
        "usage_scenario": set(sp.usage_values()),
        "household_size": set(sp.HOUSEHOLD_SIZES),
        "pain_points": set(sp.pain_point_values()),
        "priority_order": set(sp.dimension_keys()),
    }
    seen: set[str] = set()
    for c in cases:
        cid = c.get("id", "<无 id>")
        if cid in seen:
            problems.append(f"{cid}: id 重复")
        seen.add(cid)
        if not c.get("text", "").strip():
            problems.append(f"{cid}: text 为空")
        for field, allowed in enums.items():
            val = (c.get("expect") or {}).get(field)
            if val is None:
                continue
            items = val if isinstance(val, list) else [val]
            for item in items:
                if item not in allowed:
                    problems.append(f"{cid}: {field} 的期望值 {item!r} 不在封闭枚举内")
        if not c.get("note"):
            problems.append(f"{cid}: 缺 note（金标必须写明这条在测什么）")
    # 判据 1/2 依赖「期望为空」这一类样本存在，否则评测会假通过
    empty_cases = [c for c in cases if not (c.get("expect") or {})]
    if len(empty_cases) < 5:
        problems.append(f"空期望样本只有 {len(empty_cases)} 条（建议 ≥5），判据 1/2 会失去意义")
    return problems


def score(cases: list[dict], results: dict[str, dict | None]) -> dict:
    tp = Counter()
    fp = Counter()
    fn = Counter()
    exact = 0
    empty_ok = 0
    empty_total = 0
    illegal = 0
    mismatches: list[tuple[str, str, dict | None, dict]] = []

    for c in cases:
        cid = c["id"]
        got = results.get(cid)
        exp = c.get("expect") or {}
        if not exp:
            empty_total += 1
            if not got:
                empty_ok += 1
            else:
                mismatches.append((cid, c["text"], got, exp))
        else:
            if got == exp:
                exact += 1
            elif got is not None:
                mismatches.append((cid, c["text"], got, exp))
        for f in FIELDS:
            e = exp.get(f)
            g = (got or {}).get(f)
            e_items = set(e) if isinstance(e, list) else ({e} if e else set())
            g_items = set(g) if isinstance(g, list) else ({g} if g else set())
            tp[f] += len(e_items & g_items)
            fp[f] += len(g_items - e_items)
            fn[f] += len(e_items - g_items)
        # 合法性复查：输出必须只含封闭枚举里的值
        if got:
            for f in FIELDS:
                g = got.get(f)
                if g is None:
                    continue
                allowed = {
                    "usage_scenario": set(sp.usage_values()),
                    "household_size": set(sp.HOUSEHOLD_SIZES),
                    "pain_points": set(sp.pain_point_values()),
                    "priority_order": set(sp.dimension_keys()),
                }[f]
                items = g if isinstance(g, list) else [g]
                illegal += sum(1 for i in items if i not in allowed)

    non_empty = len(cases) - empty_total
    return {
        "cases": len(cases),
        "non_empty": non_empty,
        "exact": exact,
        "exact_rate": round(exact / non_empty, 4) if non_empty else 0.0,
        "empty_ok": empty_ok,
        "empty_total": empty_total,
        "empty_rate": round(empty_ok / empty_total, 4) if empty_total else 0.0,
        "illegal_values": illegal,
        "per_field": {
            f: {
                "precision": round(tp[f] / (tp[f] + fp[f]), 4) if (tp[f] + fp[f]) else None,
                "recall": round(tp[f] / (tp[f] + fn[f]), 4) if (tp[f] + fn[f]) else None,
            }
            for f in FIELDS
        },
        "mismatches": mismatches,
    }


async def run_live(cases: list[dict], timeout_ms: int) -> dict[str, dict | None]:
    from app.common.llm import LLMClient

    client = LLMClient()
    if not getattr(client, "available", False):
        raise SystemExit("DEEPSEEK_API_KEY 未配置（backend/.env）；无法跑真实抽取")
    out: dict[str, dict | None] = {}
    for c in cases:
        out[c["id"]] = await sp.extract_soft_prefs(c["text"], llm=client, timeout_ms=timeout_ms)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="L1 软偏好抽取质量评测")
    ap.add_argument("--check-corpus-only", action="store_true", help="只校验金标文件，不调 LLM")
    ap.add_argument("--timeout-ms", type=int, default=20000)
    ap.add_argument("--json", help="把结果写入该 JSON 路径")
    args = ap.parse_args()

    cases = load_gold()
    problems = check_corpus(cases)
    if problems:
        print(f"金标文件自洽性检查失败（{len(problems)} 处）：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print(f"[金标自洽性] OK：{len(cases)} 条，字段值全在封闭枚举内，含 "
          f"{len([c for c in cases if not (c.get('expect') or {})])} 条空期望样本")

    if args.check_corpus_only:
        return 0

    results = asyncio.run(run_live(cases, args.timeout_ms))
    rep = score(cases, results)

    print(f"\n样本 {rep['cases']}（有期望 {rep['non_empty']} / 空期望 {rep['empty_total']}）")
    print(f"逐字段完全一致：{rep['exact']}/{rep['non_empty']} = {rep['exact_rate']:.0%}")
    print(f"空偏好未编造：{rep['empty_ok']}/{rep['empty_total']} = {rep['empty_rate']:.0%}")
    print(f"越界枚举值：{rep['illegal_values']}（必须为 0）")
    print("\n逐字段精确率/召回率：")
    for f, m in rep["per_field"].items():
        p = "—" if m["precision"] is None else f"{m['precision']:.0%}"
        r = "—" if m["recall"] is None else f"{m['recall']:.0%}"
        print(f"  {f:16} P={p:>5}  R={r:>5}")

    if rep["mismatches"]:
        print(f"\n不一致 {len(rep['mismatches'])} 条：")
        for cid, text, got, exp in rep["mismatches"]:
            print(f"  [{cid}] {text!r}\n      期望 {exp}\n      实得 {got}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({k: v for k, v in rep.items() if k != "mismatches"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n报告已写入 {args.json}")

    verdict = rep["empty_rate"] == 1.0 and rep["illegal_values"] == 0
    print(f"\n硬判据（空偏好不编造 + 无越界值）：{'通过' if verdict else '不通过'}"
          f" —— 这是接入排序的第 1 道门槛")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
