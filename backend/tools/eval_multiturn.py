"""多轮对话场景评测：跨轮行为的确定性判据。

## 为什么需要

单轮评测测的是「这一轮答得对不对」，而**个性化功能的正确性几乎全在跨轮之间**：

    拒过的那台，下一轮还推不推？
    上次说过的预算，这一轮还在用吗？
    删掉的记忆，这一轮是不是真的没了？

这三件事单轮题一条都测不出来——它们需要「多轮上下文 + 跨轮观察」。

## 判据：二元、确定性、零 judge

每个场景是「若干轮 + 每轮的断言」。断言只用**回答文本**判定，不需要 LLM judge：

    not_recommended   回答里不应出现该车系（按车系名匹配）
    recommended       回答里应出现该车系
    mentions          回答里应提到某关键词
    not_mentions      回答里不应提到某关键词

「这台车在不在推荐列表里」是**可枚举的事实**，不是需要理解语义的判断——
用 judge 反而引入噪声与成本。仓库既有纪律也要求「零人工标注」。

## 锚点用稳定字符串，不用数字 id

2026-10-20 实测：单轮题库 410 道题里，**406 道的 `anchors.series_id`
指向的车与题面说的不是同一台**（库重导入后 id 重排）。若多轮框架沿用 id 锚点，
会**继承同一个缺陷**。故本框架的 `car` 一律用**车系名**。

## 用法

    python tools/eval_multiturn.py --scenarios eval/multiturn.json --report eval/multiturn-report.json
    python tools/eval_multiturn.py --scenarios eval/multiturn.json --scenario reject_then_no_recommend

需要 `DATABASE_URL` 指向**当前生产库**。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path)
from sqlalchemy.orm import Session

from app.agent.engine import get_agent_engine
from app.agent.session import get_session_store
from app.common.database import get_session_factory

DEFAULT_SCENARIOS = "eval/multiturn.json"

#: 断言类型 → 人类可读说明（报告里用）
ASSERT_KINDS = {
    "not_recommended": "回答不应包含该车系",
    "recommended": "回答应包含该车系",
    "mentions": "回答应提到该关键词",
    "not_mentions": "回答不应提到该关键词",
}


class AssertionFailed(Exception):
    """一条断言不成立。"""


def _norm(text: str) -> str:
    """归一化：去空白、小写。用于车系名/关键词的宽松匹配。"""
    return "".join((text or "").split()).lower()


def _recommended_names(out: Any) -> list[str]:
    """本轮**实际推荐**的车系名（结构化字段，不是文本匹配）。

    `AgentMessageOut.recommended_variants` 每项都有 `series_name` 与 `brand_name`，
    比在 `explanation` 散文里找车系名**可靠得多**——散文里可能提到没推荐的车
    （「也可以看看 XX」），而结构化字段只含**真正进了推荐列表**的车。
    """
    names: list[str] = []
    for v in getattr(out, "recommended_variants", None) or []:
        sname = getattr(v, "series_name", None)
        bname = getattr(v, "brand_name", None)
        if sname:
            names.append(str(sname))
        if bname and sname:
            names.append(f"{bname}{sname}")
    return names


def _answer_text(out: Any) -> str:
    """本轮的全部可见文本：解释 + 理由 + 取舍 + 追问。

    关键词类断言（mentions / not_mentions）看这个；
    **车系类断言不看这个**——看 `recommended_variants`。
    """
    parts: list[str] = []
    for attr in ("explanation",):
        v = getattr(out, attr, None)
        if v:
            parts.append(str(v))
    for attr in ("reasons", "tradeoffs"):
        for v in getattr(out, attr, None) or []:
            parts.append(str(v))
    clar = getattr(out, "clarification", None)
    if clar is not None and getattr(clar, "question", None):
        parts.append(str(clar.question))
    follow = getattr(out, "followup", None)
    if follow is not None and getattr(follow, "question", None):
        parts.append(str(follow.question))
    return "\n".join(parts)


def _car_needles(car: str, db: Session | None = None) -> list[str]:
    """车系名在推荐列表里可能出现的所有写法。

    回答里通常带品牌前缀（「比亚迪 汉」），而题面里往往只写短名（「汉」）。
    所以两种写法都要算命中，否则 `not_recommended` 会在「回答写了全名」时漏判。

    带 db 时把库内的**别名**也算进来——推荐卡片可能用别名显示。
    """
    needles = {_norm(car)}
    if not car:
        return [n for n in needles if n]
    if db is not None:
        from sqlalchemy import select

        from app.common.models import Brand, VehicleSeries

        row = db.execute(
            select(VehicleSeries.name, VehicleSeries.aliases, Brand.name)
            .join(Brand, Brand.id == VehicleSeries.brand_id)
            .where(VehicleSeries.name == car)
        ).first()
        if row is not None:
            name, aliases, brand_name = row
            needles.add(_norm(name))
            if brand_name:
                needles.add(_norm(f"{brand_name}{name}"))
            for alias in aliases or []:
                if alias:
                    needles.add(_norm(alias))
    return [n for n in needles if n]


def check_assertion(
    out: Any, assertion: dict, db: Session | None = None
) -> None:
    """执行一条断言；不成立则抛 `AssertionFailed`。

    ## 判据分两类，用不同的证据

    - **车系类**（`recommended` / `not_recommended`）：看
      `recommended_variants` 的**结构化** `series_name`——
      它只含**真正进了推荐列表**的车。散文里提到但没推荐的车**不算**。
    - **关键词类**（`mentions` / `not_mentions`）：看本轮全部可见文本
      （解释 + 理由 + 取舍 + 追问）。
    """
    kind = assertion.get("type")
    if kind not in ASSERT_KINDS:
        raise AssertionFailed(f"未知的断言类型: {kind!r}")

    if kind in ("not_recommended", "recommended"):
        car = assertion.get("car")
        if not car:
            raise AssertionFailed(f"{kind} 断言缺 car 字段")
        needles = _car_needles(car, db)
        recommended = [_norm(n) for n in _recommended_names(out)]
        hit = any(n in recommended for n in needles)
        if kind == "not_recommended" and hit:
            raise AssertionFailed(f"{kind}：{assertion!r} —— 但它进了推荐列表")
        if kind == "recommended" and not hit:
            raise AssertionFailed(f"{kind}：{assertion!r} —— 但它不在推荐列表里")
        return

    word = assertion.get("word")
    if not word:
        raise AssertionFailed(f"{kind} 断言缺 word 字段")
    hit = _norm(word) in _norm(_answer_text(out))
    if kind == "not_mentions" and hit:
        raise AssertionFailed(f"{kind}：{assertion!r} —— 但回答里提到了")
    if kind == "mentions" and not hit:
        raise AssertionFailed(f"{kind}：{assertion!r} —— 但回答里没提到")


async def run_scenario(
    db: Session, scenario: dict, *, llm_available: bool = True
) -> dict:
    """跑一个多轮场景，返回结果记录。

    每轮用**同一个 session_id**——跨轮行为的前提就是会话态连续。
    """
    engine = get_agent_engine()
    store = get_session_store()
    session_id = store.create()

    turn_results: list[dict] = []
    failures: list[str] = []
    started = time.perf_counter()
    # 本场景是否产出过推荐——用于**自检**（见返回值的 `no_recommendation`）。
    any_recommendation = False

    for idx, turn in enumerate(scenario.get("turns") or [], start=1):
        user = turn.get("user") or ""
        try:
            out = await engine.handle(db, session_id, user)
        except Exception as exc:  # noqa: BLE001 - 场景失败要记下来，不能让一个场景崩掉整轮
            failures.append(f"第 {idx} 轮异常: {type(exc).__name__}: {exc}")
            turn_results.append({"turn": idx, "user": user, "error": type(exc).__name__})
            break

        recommended = _recommended_names(out)
        if recommended:
            any_recommendation = True

        turn_failures = []
        for assertion in turn.get("assert") or []:
            try:
                check_assertion(out, assertion, db)
            except AssertionFailed as exc:
                turn_failures.append(str(exc))
        failures.extend(f"第 {idx} 轮：{f}" for f in turn_failures)

        turn_results.append(
            {
                "turn": idx,
                "user": user,
                "answer": _answer_text(out),
                "recommended": recommended,
                "need_clarification": bool(getattr(out, "need_clarification", False)),
                "assertions": len(turn.get("assert") or []),
                "failed_assertions": turn_failures,
            }
        )

    # ⚠️ **空转自检**：若整场对话**一次都没推荐出车**，
    # 那么 `not_recommended` 断言是**恒真的**——它「通过」不是因为行为正确，
    # 而是因为什么都没发生。这种场景必须标成**无效**，不能计入通过。
    # 这正是本仓反复记录的「不可能变红」陷阱的另一种形态。
    no_recommendation = not any_recommendation
    if no_recommendation and scenario.get("turns"):
        failures.append(
            "本场景全程未产出任何推荐——`not_recommended` 断言恒真，场景无效"
            "（检查：库内该价位/车身/能源档是否真有在售车系）"
        )

    return {
        "id": scenario.get("id"),
        "scenario": scenario.get("scenario"),
        "turns": len(scenario.get("turns") or []),
        "passed": not failures,
        "no_recommendation": no_recommendation,
        "failures": failures,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "turn_results": turn_results,
    }


def load_scenarios(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    payload = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return list(payload.get("scenarios") or [])
    return list(payload)


async def _run_all(
    session: Session, scenarios: list[dict]
) -> tuple[list[dict], Counter]:
    results: list[dict] = []
    by_kind: Counter = Counter()
    for sc in scenarios:
        res = await run_scenario(session, sc)
        results.append(res)
        if res.get("no_recommendation"):
            label = "INVALID(无推荐→断言恒真)"
        else:
            label = "PASS" if res["passed"] else "FAIL"
        by_kind[f"{res['scenario']}:{label}"] += 1
    return results, by_kind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多轮对话场景评测")
    parser.add_argument("--scenarios", default=DEFAULT_SCENARIOS, help="场景文件路径")
    parser.add_argument("--report", default="", help="报告输出路径（JSON）")
    parser.add_argument("--scenario", default="", help="只跑某个场景类型")
    parser.add_argument("--limit", type=int, default=0, help="最多跑多少个场景")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    if not scenarios:
        print(f"没有场景可跑（{args.scenarios}）", file=sys.stderr)
        return 2
    if args.scenario:
        scenarios = [s for s in scenarios if s.get("scenario") == args.scenario]
    if args.limit:
        scenarios = scenarios[: args.limit]

    session = get_session_factory()()
    try:
        results, by_kind = asyncio.run(_run_all(session, scenarios))
    finally:
        session.close()

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    print(f"多轮场景：{passed}/{total} 通过")
    for k in sorted(by_kind):
        print(f"  {k}: {by_kind[k]}")

    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(
            json.dumps(
                {"total": total, "passed": passed, "by_kind": dict(by_kind), "results": results},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"报告：{rp}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
