"""记忆的**边际价值**评估（2026-10-21）

## 它回答什么

计划 §七的第一条未验证判断：

> 「硬约束排除项是记忆的第一价值」——产品直觉，未做用户测试。

本工具给出它在**合成会话**上的**机制层**证据：同一批跨会话场景，
**记忆开**（`DURABLE_PREFS_MODE=on`）与**记忆关**（`off`）各跑一遍，
比较两类下��通过率（= 1 − 违规率）的**差**。

- 差越大 ⇒ 这一类记忆在机制上**越值钱**（关掉它，违规就回来了）。
- 差为 0 ⇒ 两种解释，必须分开看：
  (a) 这类记忆**没起作用**；
  (b) 这类场景**不构成对照**（关掉记忆也照样通过 ⇒ 断言恒真，验不到任何东西）。
  本工具用 `off` 组的通过率把这两者**分开**并明确标出来——本仓的规矩是
  「恒真的断言不算通过」。

## 为什么是合成会话（而不是生产日志）

2026-10-21 实测生产库：`agent_turn_logs` **0 行**；近 14 天 nginx **43,660** 条请求里，
agent 端点只有 `GET /api/v1/agent/sessions`（开面板建会话），
**消息 POST 次数为 0** ⇒ 生产至今**没有过一次真实对话**，
「真实用户测试」这个最终判据当前**无从谈起**。
所以本工具用本地合成会话做**机制验证**，并在报告里明确写清这条边界。

## 用法

```bash
cd backend
export DATABASE_URL="sqlite:///.tmp/eval-local.db"
export RETRIEVAL_BACKEND=inmemory
python tools/gen_multiturn_scenarios.py --out .tmp/corpus.json --enable-memory-scenarios
python tools/eval_memory_value.py --scenarios .tmp/corpus.json --report .tmp/memory_value.json
```

退出码 0 = 两组都跑完（**不**要求 on 组全绿：off 组红才是记忆在起作用的证据）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from app.agent.preferences import MODE_ENV
from app.common.database import get_session_factory
from tools.eval_multiturn import load_scenarios, run_scenario

#: 只跑**跨会话记忆**的场景——会话内的拒绝不算（它本来就是设计如此）。
MEMORY_SCENARIOS = (
    "returning_user_memory",
    "memory_deleted",
    "rejection_remembered_across_sessions",
)

#: 场景类型 → 它检验的**记忆类型**（用于报告里的分列）。
MEMORY_TYPE = {
    "returning_user_memory": "预算（回访记忆生效）",
    "memory_deleted": "预算（记忆删除后不生效）",
    "rejection_remembered_across_sessions": "拒绝/排除（跨会话还记得）",
}

MODES = ("on", "off")


def classify(on_rate: float, off_rate: float) -> str:
    """判定一类的记忆有没有在起作用。

    三种结论必须**分开**，不能把「没起作用」和「不构成对照」混为一谈：

    - `off_rate >= 1.0`：关掉记忆也全过 ⇒ 断言**恒真** ⇒ 这类场景验不到任何东西；
    - `delta <= 0`：记忆开着也没比关着好 ⇒ 记忆**没起作用**；
    - 否则：记忆**有效**。
    """
    if off_rate >= 1.0:
        return "不构成对照（关掉记忆也全过 ⇒ 断言恒真）"
    if on_rate - off_rate <= 0:
        return "记忆未带来改善"
    return "记忆有效"


def _run(db: object, scenarios: list[dict]) -> list[dict]:
    """跑一批场景（同步外壳）。`run_scenario` 内部自取引擎与身份。"""
    out: list[dict] = []
    for sc in scenarios:
        out.append(asyncio.run(run_scenario(db, sc)))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记忆的边际价值评估（on/off 对照）")
    parser.add_argument("--scenarios", required=True, help="场景文件路径")
    parser.add_argument("--report", default="", help="报告输出路径（JSON）")
    parser.add_argument("--limit", type=int, default=0, help="每类最多跑多少个场景（0=不限）")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    if not scenarios:
        print(f"没有场景可跑（{args.scenarios}）", file=sys.stderr)
        return 2
    picked = [s for s in scenarios if s.get("scenario") in MEMORY_SCENARIOS]
    if not picked:
        print(
            "题库里没有跨会话记忆场景——先跑 gen_multiturn_scenarios.py --enable-memory-scenarios",
            file=sys.stderr,
        )
        return 2
    by_type: dict[str, list[dict]] = defaultdict(list)
    for s in picked:
        by_type[str(s.get("scenario"))].append(s)
    if args.limit:
        for k in by_type:
            by_type[k] = by_type[k][: args.limit]

    # 身份隔离：每个场景新建一个匿名身份 ⇒ on 组写下的记忆**不会**漏进 off 组。
    # （`get_or_create_identity` 每次调用都新建，不复用 token。）
    session = get_session_factory()()
    results: dict[str, list[dict]] = {}
    try:
        for mode in MODES:
            # get_mode() 每次调用现读环境变量 ⇒ 同一进程内可切换
            os.environ[MODE_ENV] = mode
            results[mode] = _run(session, picked)
    finally:
        session.close()
        os.environ.pop(MODE_ENV, None)

    def stats(mode: str) -> dict[str, dict[str, int]]:
        agg: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
        for r in results[mode]:
            t = str(r.get("scenario"))
            agg[t]["total"] += 1
            if r.get("passed"):
                agg[t]["passed"] += 1
        return agg

    on_stats, off_stats = stats("on"), stats("off")

    print("=" * 72)
    print("记忆的边际价值（合成会话；记忆开 vs 记忆关）")
    print("=" * 72)
    print(f"{'记忆类型':<28} {'场景':<6} {'on 通过':>10} {'off 通过':>10} {'Δ':>8}  判定")
    rows: list[dict] = []
    for t in sorted(by_type):
        o, f = on_stats.get(t, {"total": 0, "passed": 0}), off_stats.get(t, {"total": 0, "passed": 0})
        if not o["total"]:
            continue
        on_rate = o["passed"] / o["total"]
        off_rate = f["passed"] / f["total"]
        delta = on_rate - off_rate
        verdict = classify(on_rate, off_rate)
        rows.append(
            {
                "scenario": t,
                "memory_type": MEMORY_TYPE.get(t, t),
                "total": o["total"],
                "on_passed": o["passed"],
                "off_passed": f["passed"],
                "on_rate": round(on_rate, 4),
                "off_rate": round(off_rate, 4),
                "delta": round(delta, 4),
                "verdict": verdict,
            }
        )
        print(
            f"{MEMORY_TYPE.get(t, t):<28} {o['total']:>4}  "
            f"{o['passed']:>4}/{o['total']:<5} {f['passed']:>4}/{f['total']:<5} "
            f"{delta:>+8.2%}  {verdict}"
        )

    reasons: Counter = Counter()
    for mode in MODES:
        for r in results[mode]:
            for failure in r.get("failures") or []:
                reasons[f"{mode}: {failure}"] += 1
    if reasons:
        print("\n失败原因（前 10 条）：")
        for reason, n in reasons.most_common(10):
            print(f"  {n:>4} × {reason[:120]}")

    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(
            json.dumps(
                {
                    "note": (
                        "合成会话上的机制层证据；真实用户测试仍缺"
                        "（2026-10-21 实测：生产 0 次真实 agent 对话）"
                    ),
                    "scenarios_file": args.scenarios,
                    "by_memory_type": rows,
                    "failures": dict(reasons),
                    "results": results,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\n报告：{rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
