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
4. **零封闭失效**：模型在原始响应里说出封闭词表之外的东西（`out_of_enum` /
   `bad_type` / `bad_format`）。

## 越界是怎么统计的（2026-10-04 改，别再改回旧写法）

原先第 4 项是对 sanitize **之后**的输出复查「值是否在词表内」——而
`sanitize` → `_pick_value` **已经把词表外的值丢掉了**，所以那个计数
**必然为 0**：它是构造性的，不是「模型没越界」的证据。

现在改为消费 `extract_soft_prefs` 的 `illegal_out`：越界值在**被丢弃的那一刻**
被记下来，带字段归属与原因，与线上 `log_shadow` **同源同口径**。

配套三条与线上聚合器一致的纪律：
- 越界率分母**只算 LLM 响应过的样本**（超时/空内容不算）；
- 拼错或缺字段一律记 `unknown`，**不得**默认「干净」；
- 没观测到的样本计入 `unobserved`，**不参与**「通过」判定。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import soft_prefs as sp  # noqa: E402

GOLD = Path(__file__).resolve().parents[1] / "tests" / "soft_prefs_golden.jsonl"
FIELDS = ("usage_scenario", "household_size", "pain_points", "priority_order")

# 与线上 `soft_prefs_report` **同源**的两个闸门常量。两边必须一起改——
# 「离线比线上宽松」会让离线绿灯掩盖线上红灯，那比没有离线评测更糟。
_MIN_SAMPLE = 20
_MAX_EMPTY_RATE = 0.5


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


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
    violated = 0
    status_counter: Counter = Counter()
    reason_counter: Counter = Counter()
    field_counter: Counter = Counter()
    unobserved = 0
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
        # 越界**不再**在这里复查 sanitize 后的 `got`——那个计数必然为 0
        # （`_pick_value` 已把词表外的值丢掉），是构造性的。
        # 改用 `extract_soft_prefs` 在**丢弃那一刻**记下的观测（见 collect_observations），
        # 与线上 `log_shadow` 同源。
        obs = _OBSERVATIONS.get(cid)
        if obs is None:
            # 没有观测 = 没见过这一条（如 --check-corpus-only 或外部传入的 results）。
            # 不得默认「干净」——按不可判定计入。
            unobserved += 1
            continue
        status = obs.get("status", "unknown")
        status_counter[status] += 1
        entries = obs.get("illegal") or []
        if entries:
            # 「越界率」按**样本**算，不按条目算：一条样本可以有多条失效，
            # 按条目算会出现 >100% 的荒谬数字（金标 25 条实测 33 条 → 132%）。
            violated += 1
        for item in entries:
            illegal += 1
            reason_counter[str(item.get("reason", "?"))] += 1
            field_counter[str(item.get("field", "?"))] += 1

    non_empty = len(cases) - empty_total
    # 越界率的分母只算 LLM **真的响应过**的样本：把「超时 / 空内容」也算进去的话，
    # 一次大面积超时会显示成「越界率 0」——不是模型守规矩，是它根本没被调用上。
    responded = status_counter.get("ok", 0) + status_counter.get("all_rejected", 0)
    illegal_rate = round(violated / responded, 4) if responded else None
    all_rejected = status_counter.get("all_rejected", 0)
    # 空偏好率：响应过的样本里「一条字段都没通过」的比例。
    # 全部 all_rejected 却零越界，看起来是完美通过——其实模型每条都在说废话、
    # 一次都没按 schema 思考，「越界率 0」毫无意义（与线上 `_MAX_EMPTY_RATE` 同理）。
    response_empty_rate = round(all_rejected / responded, 4) if responded else None
    return {
        "cases": len(cases),
        "non_empty": non_empty,
        "exact": exact,
        "exact_rate": round(exact / non_empty, 4) if non_empty else 0.0,
        "empty_ok": empty_ok,
        "empty_total": empty_total,
        "empty_rate": round(empty_ok / empty_total, 4) if empty_total else 0.0,
        # 越界：真实观测。`violated_samples` 用于算率，`illegal_values` 是条目数。
        "violated_samples": violated,
        "illegal_values": illegal,
        "illegal_rate": illegal_rate,
        "illegal_by_reason": dict(reason_counter.most_common()),
        "illegal_by_field": dict(field_counter.most_common()),
        "status_distribution": dict(status_counter.most_common()),
        "responded": responded,
        "unobserved": unobserved,
        # 与线上聚合器 `soft_prefs_report` 对齐的两个闸门：
        "all_rejected": all_rejected,
        "response_empty_rate": response_empty_rate,
        "min_sample": _MIN_SAMPLE,
        "max_empty_rate": _MAX_EMPTY_RATE,
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
    # 必须先 clear：只 `update` 的话，进程内跑第二遍时上一轮的 case 仍留在表里，
    # id 撞车就会把**旧**的 illegal 计入**新**报告。CLI 单次无害，脚本里连跑就会中招。
    _OBSERVATIONS.clear()
    _OBSERVATIONS.update(await collect_observations(cases, timeout_ms, out, client))
    return out


#: 观测结果：`case_id -> {"status": str, "illegal": list}`。
#: 用模块级 dict 是为了让 `score` 保持原签名（它只吃 cases + results）——
#: `run_live` 与 `score` 之间的这条带子是一次显式的旁路，不是隐式全局状态。
_OBSERVATIONS: dict[str, dict] = {}


async def collect_observations(
    cases: list[dict],
    timeout_ms: int,
    out: dict[str, dict | None],
    client: Any,
) -> dict[str, dict]:
    """跑抽取的同时收集**封闭失效观测**，与线上走**完全同一条通道**。

    ## 为什么必须这样，而不是在 `score` 里复查一遍

    旧实现在 `score` 里对 `got`（= sanitize **之后**的输出）复查「值是否在词表内」。
    而 `sanitize` → `_pick_value` **已经把词表外的值丢掉了**，所以那个计数
    **必然为 0**——它是构造性的，不是「模型没越界」的证据。

    这里改为直接消费 `extract_soft_prefs` 的 `illegal_out` / `status_out`：
    越界值在**被丢弃的那一刻**就被记下来，带字段归属与原因
    （`out_of_enum` / `bad_type` / `bad_format`），与 `log_shadow` 同源。

    好处不止是「数字变真」：**线上与离线从此只有一套口径**，
    不存在「线上按 A 算、离线按 B 算」而两边对不上的可能。
    """
    observations: dict[str, dict] = {}
    for c in cases:
        illegal: list[dict[str, Any]] = []
        status: list[str] = []
        out[c["id"]] = await sp.extract_soft_prefs(
            c["text"], llm=client, timeout_ms=timeout_ms,
            illegal_out=illegal, status_out=status,
        )
        observations[c["id"]] = {
            "status": status[0] if len(status) == 1 else "unknown",
            "illegal": illegal,
        }
    return observations


def verdict_of(rep: dict) -> tuple[list[str], bool]:
    """报告 → (阻塞原因列表, 是否通过)。

    抽成独立函数是为了能**不调 LLM** 就单测判定逻辑——判据本身是最该被测的部分，
    却因为埋在 `main()` 里（要真机才跑得到）而长期无人验证。

    四道闸门与线上聚合器 `soft_prefs_report` **同口径**：离线比线上宽松，
    就会让离线绿灯掩盖线上红灯，那比没有离线评测更糟。
    """
    blockers: list[str] = []
    if rep.get("empty_rate") != 1.0:
        blockers.append("空偏好有编造")
    if rep.get("violated_samples"):
        blockers.append(
            f"越界 {rep['violated_samples']}/{rep.get('responded', 0)} 条样本"
            f"（{_pct(rep.get('illegal_rate'))}，条目 {rep.get('illegal_values', 0)} 条）"
        )
    if rep.get("unobserved"):
        blockers.append(f"{rep['unobserved']} 条样本无观测")
    no_response = (rep.get("status_distribution") or {}).get("no_response", 0)
    if no_response:
        blockers.append(f"{no_response} 条 LLM 未调通（超时不计入越界率分母）")
    empty_rate = rep.get("response_empty_rate")
    if empty_rate is not None and empty_rate > rep.get("max_empty_rate", _MAX_EMPTY_RATE):
        blockers.append(
            f"空偏好率 {_pct(empty_rate)} 超过 "
            f"{rep.get('max_empty_rate', _MAX_EMPTY_RATE):.0%}"
            "——模型每条都在说废话却从不越界，说明它没在按 schema 思考，"
            "「越界率 0」没有意义"
        )
    if rep.get("responded", 0) < rep.get("min_sample", _MIN_SAMPLE):
        blockers.append(
            f"样本不足：响应样本 {rep.get('responded', 0)} 条 "
            f"< 下限 {rep.get('min_sample', _MIN_SAMPLE)} 条"
        )
    return blockers, not blockers


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
    print(f"整条记录完全一致：{rep['exact']}/{rep['non_empty']} = {rep['exact_rate']:.0%}"
          "（判的是 got == exp，不是逐字段）")
    print(f"空偏好未编造：{rep['empty_ok']}/{rep['empty_total']} = {rep['empty_rate']:.0%}")
    rate = rep["illegal_rate"]
    print(f"**封闭失效（越界）**：{rep['violated_samples']}/{rep['responded']} 条样本 = "
          f"{_pct(rate)}（分母 = LLM 响应过的样本）；条目 {rep['illegal_values']} 条")
    if rep["unobserved"]:
        print(f"⚠️ 无观测的样本 {rep['unobserved']} 条——按不可判定计入，**不得**默认「干净」")
    dist = rep["status_distribution"]
    if dist:
        print("LLM 响应状态：" + " / ".join(f"{k}={v}" for k, v in sorted(dist.items())))
    if rep["illegal_by_reason"]:
        print("按原因：" + " / ".join(f"{k}={v}" for k, v in rep["illegal_by_reason"].items()))
    if rep["illegal_by_field"]:
        print("按字段：" + " / ".join(f"{k}={v}" for k, v in rep["illegal_by_field"].items()))
    empty_rate = rep["response_empty_rate"]
    if empty_rate is not None:
        flag = "  ⚠️ 超过上限" if empty_rate > _MAX_EMPTY_RATE else ""
        print(f"响应内空偏好率（一条字段都没通过）：{_pct(empty_rate)}{flag}")
    if rep["responded"] < _MIN_SAMPLE:
        print(f"⚠️ 响应样本 {rep['responded']} 条 < 下限 {_MIN_SAMPLE} 条——仅供参考，不足以支撑切流判据")
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

    # 判定逻辑在 verdict_of()（可脱离 LLM 单测），此处只负责呈现
    blockers, verdict = verdict_of(rep)
    print(f"\n硬判据（空偏好不编造 + 零越界 + 全部样本有观测"
          f" + 空偏好率/样本量不越线）：{'通过' if verdict else '不通过'}"
          + ("" if verdict else "——" + "；".join(blockers))
          + " —— 这是接入排序的第 1 道门槛")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
