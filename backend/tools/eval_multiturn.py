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
    budget_in         本轮采用的**预算口径**应是该区间（查 response.filters，不查正文）
    budget_not_in     本轮采用的预算口径不应是该区间

「这台车在不在推荐列表里」是**可枚举的事实**，不是需要理解语义的判断——
用 judge 反而引入噪声与成本。仓库既有纪律也要求「零人工标注」。

## 预算类断言为什么查**结构化口径**（2026-10-21）

预算值在正文里必然与**车价**混在一起：12-18 万档里真实存在 12.99 万 / 12.68 万的车，
于是裸字符串 `not_mentions '12'` 会把**正确答案**判成失败（假否定 ⇒ 低估能力），
`mentions '10'` 也会因推荐了 8.28 万的车而判失败。两个方向都会误判 ⇒ 改查
`response.filters` 的 `budget_min` / `budget_max`。

## 锚点用稳定字符串，不用数字 id

2026-10-20 实测：单轮题库 410 道题里，**406 道的 `anchors.series_id`
指向的车与题面说的不是同一台**（库重导入后 id 重排）。若多轮框架沿用 id 锚点，
会**继承同一个缺陷**。故本框架的 `car` 一律用**车系名**。

## 跨会话：每轮可带 `session` 标签

2026-10-21 补：题库里的「回访用户记忆」「删掉的记忆」两个场景要测的是**跨会话**行为，
而此前框架全程共用一个 `session_id` ⇒ 测的其实是「同会话内还记不记得」
（记忆要么一直生效、要么一直不生效，**都不是跨会话**）。

现在每轮可带 `session` 标签（"first"/"second"…）：**标签变了就换一个 session**；
同一标签的多轮仍在同一会话。跨会话场景还要一个**身份**（记忆的载体），
引擎支持 `handle(identity=...)` 时本场建一个、所有会话共用。

## 只跑 `enabled` 的场景

题库里 `enabled: false` 的场景依赖**尚未落地**的能力。框架默认跳过它们
（单独报数、不计入分母）——否则「通过率」被未落地的能力拉低，
看报告的人会误判成「功能坏了」，更糟的是有人会去**改断言**让它变绿。
要强行跑看它为什么红：`--include-disabled`。

## 用法

    python tools/eval_multiturn.py --scenarios eval/multiturn.json --report eval/multiturn-report.json
    python tools/eval_multiturn.py --scenarios eval/multiturn.json --scenario reject_then_no_recommend

需要 `DATABASE_URL` 指向**当前生产库**。
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
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
    "budget_in": "回答采用的预算口径应是该区间",
    "budget_not_in": "回答采用的预算口径不应是该区间",
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


def _budget_range(out: Any) -> tuple[float | None, float | None]:
    """本轮回答采用的**预算口径**（结构化字段：`AgentMessageOut.filters`）。

    为什么不用正文里的数字：预算值在正文里必然与**车价**混在一起
    （12-18 万档里有 12.99 万的车，答案里合法地出现「12」），
    拿裸字符串判「提到/没提到预算」会**双向误判**——
    「不提到 12」会把正确答案判成失败，「提到 10」也会因为推荐了 8.28 万而判失败。
    """
    filters = getattr(out, "filters", None) or {}
    lo = filters.get("budget_min")
    hi = filters.get("budget_max")
    return (
        float(lo) if lo is not None else None,
        float(hi) if hi is not None else None,
    )


#: 预算口径的比较容差（元）：回答里的口径来自用户原话（8-12 万 → 80000/120000），
#: 但引擎可能按「不超过」口径微调（如上界取整到万），留 1000 元的余量。
_BUDGET_TOLERANCE = 1000.0


def _check_budget_assertion(out: Any, assertion: dict, kind: str) -> None:
    """预算类断言：查**结构化口径**，不查正文（见 `_budget_range` 的理由）。"""
    lo = assertion.get("min")
    hi = assertion.get("max")
    if lo is None or hi is None:
        raise AssertionFailed(f"{kind} 断言缺 min/max 字段")
    got_lo, got_hi = _budget_range(out)
    if got_lo is None or got_hi is None:
        matched = False
    else:
        matched = (
            abs(float(got_lo) - float(lo)) <= _BUDGET_TOLERANCE
            and abs(float(got_hi) - float(hi)) <= _BUDGET_TOLERANCE
        )
    if kind == "budget_in" and not matched:
        raise AssertionFailed(
            f"{kind}：{assertion!r} —— 但本轮预算口径是 {got_lo}~{got_hi}"
        )
    if kind == "budget_not_in" and matched:
        raise AssertionFailed(
            f"{kind}：{assertion!r} —— 但本轮预算口径仍是 {got_lo}~{got_hi}"
        )


def _car_in_names(car: str, names: set[str], db: Session | None = None) -> bool:
    """`car`（题面里的车系名）是否出现在推荐过的车系名集合里。

    用 `_car_needles` 的同一套写法（品牌前缀 / 库内别名都算命中），
    免得「断言恒真自检」与断言本身用两套匹配口径。
    """
    needles = _car_needles(car, db)
    normalized = {_norm(n) for n in names}
    return any(_norm(n) in normalized for n in needles)


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

    if kind in ("budget_in", "budget_not_in"):
        _check_budget_assertion(out, assertion, kind)
        return

    word = assertion.get("word")
    if not word:
        raise AssertionFailed(f"{kind} 断言缺 word 字段")
    hit = _norm(word) in _norm(_answer_text(out))
    if kind == "not_mentions" and hit:
        raise AssertionFailed(f"{kind}：{assertion!r} —— 但回答里提到了")
    if kind == "mentions" and not hit:
        raise AssertionFailed(f"{kind}：{assertion!r} —— 但回答里没提到")

def _session_labels(turns: list[dict]) -> set[str]:
    """本场用到的会话标签集合（空串 = 未标注 = 默认会话）。"""
    return {str(t.get("session") or "") for t in turns}


def _needs_identity(turns: list[dict]) -> bool:
    """本场是否**跨会话**（≥2 个不同标签）——只有跨会话才需要身份。"""
    return len(_session_labels(turns)) > 1


def _engine_supports_identity(engine: Any) -> bool:
    """引擎的 `handle` 是否接受 `identity` 参数。

    用 `inspect.signature` 而不是 `try: handle(..., identity=...)`：
    后者会把「不支持」变成运行期 TypeError，且已经产出了副作用。
    """
    try:
        params = inspect.signature(engine.handle).parameters
    except (TypeError, ValueError):  # pragma: no cover - 取不到签名的代理对象
        return False
    return "identity" in params


def _identity_factory() -> Any:
    """取「建身份」的函数。

    独立成函数有两个理由：

    1. **接线坏了要响**——`app.agent.preferences` 不存在时直接 ImportError，
       不能悄悄降级成「不建身份」：降级会让「跨会话记忆」场景测的其实是
       「没有记忆」，`budget_in` 断言恒真 ⇒ 又一次**假通过**。
    2. **测试能替换**——框架单测不该依赖真实的身份表。
    """
    from app.agent.preferences import get_or_create_identity

    return get_or_create_identity


async def run_scenario(
    db: Session, scenario: dict, *, llm_available: bool = True
) -> dict:
    """跑一个多轮场景，返回结果记录。

    ## 会话与身份

    - 每轮可带 `session` 标签（如 "first"/"second"）：标签变了就**换一个 session**。
      此前全程共用一个 session ⇒ 「跨会话记忆」两个场景根本跑不出真实行为
      （记忆要么一直生效、要么一直不生效，测的都不是跨会话）。
    - 引擎支持 `handle(identity=...)` 时，本场建一个**身份**（跨会话记忆的载体），
      所有会话**共用**它——这正是「回访用户」的定义。
      引擎不支持时（Phase 1 上线前）**不建**，行为与之前逐位相同。
    - 跨会话场景但引擎不支持身份 ⇒ 场景判**红**（不是通过）：
      测不了跨会话记忆 ≠ 跨会话记忆没问题。
    """
    engine = get_agent_engine()
    store = get_session_store()
    turns = scenario.get("turns") or []

    # 会话标签 → session_id：标签变了 = **另一个会话**。
    session_ids: dict[str, str] = {}

    def _session_for(label: str) -> str:
        if label not in session_ids:
            session_ids[label] = store.create()
        return session_ids[label]

    # 跨会话场景需要一个**身份**，本场所有会话共用它。
    identity = None
    identity_error = ""
    if _needs_identity(turns):
        if not _engine_supports_identity(engine):
            identity_error = (
                "本场景跨会话，但引擎 handle 不接受 identity 参数——"
                "跨会话记忆无法评测（Phase 1 未接线？）"
            )
        else:
            try:
                identity, _token, _created = _identity_factory()(db)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 接线问题要记下来，不能崩掉整轮
                identity_error = f"身份创建失败：{type(exc).__name__}: {exc}"

    turn_results: list[dict] = []
    failures: list[str] = []
    started = time.perf_counter()
    # 本场景是否产出过推荐——用于**自检**（见返回值的 `no_recommendation`）。
    any_recommendation = False
    # 断言恒真自检（第二层）：`not_recommended(X)` 只有在 **X 之前真的被推荐过**时
    # 才有意义。若 X 从未出现在推荐里，这条断言「通过」不是因为行为正确，而是因为
    # 什么都没发生——与「全程无推荐」同类，必须标成无效。
    seen_recommended: set[str] = set()
    vacuous: list[str] = []

    for idx, turn in enumerate(turns, start=1):
        user = turn.get("user") or ""
        label = str(turn.get("session") or "")
        session_id = _session_for(label)
        try:
            if identity is None:
                out = await engine.handle(db, session_id, user)
            else:
                out = await engine.handle(db, session_id, user, identity=identity)
        except Exception as exc:  # noqa: BLE001 - 场景失败要记下来，不能让一个场景崩掉整轮
            failures.append(f"第 {idx} 轮异常: {type(exc).__name__}: {exc}")
            turn_results.append(
                {"turn": idx, "user": user, "error": type(exc).__name__, "session": label}
            )
            break

        recommended = _recommended_names(out)
        if recommended:
            any_recommendation = True

        turn_failures = []
        for assertion in turn.get("assert") or []:
            if assertion.get("type") == "not_recommended":
                car = assertion.get("car")
                if car and not _car_in_names(car, seen_recommended, db):
                    vacuous.append(
                        f"第 {idx} 轮：被断言的车「{car}」此前**从未被推荐过**——"
                        f"「它不该再被推荐」恒真，断言无效"
                    )
            try:
                check_assertion(out, assertion, db)
            except AssertionFailed as exc:
                turn_failures.append(str(exc))
        failures.extend(f"第 {idx} 轮：{f}" for f in turn_failures)
        seen_recommended.update(recommended)

        turn_results.append(
            {
                "turn": idx,
                "user": user,
                "session": label,
                "answer": _answer_text(out),
                "recommended": recommended,
                "need_clarification": bool(getattr(out, "need_clarification", False)),
                "assertions": len(turn.get("assert") or []),
                "failed_assertions": turn_failures,
            }
        )

    if identity_error:
        failures.append(identity_error)

    # ⚠️ **空转自检**：若整场对话**一次都没推荐出车**，
    # 那么 `not_recommended` 断言是**恒真的**——它「通过」不是因为行为正确，
    # 而是因为什么都没发生。这种场景必须标成**无效**，不能计入通过。
    # 这正是本仓反复记录的「不可能变红」陷阱的另一种形态。
    no_recommendation = not any_recommendation
    if no_recommendation and turns:
        failures.append(
            "本场景全程未产出任何推荐——`not_recommended` 断言恒真，场景无效"
            "（检查：库内该价位/车身/能源档是否真有在售车系）"
        )
    failures.extend(vacuous)

    return {
        "id": scenario.get("id"),
        "scenario": scenario.get("scenario"),
        "turns": len(turns),
        "sessions": len(session_ids),
        "cross_session": _needs_identity(turns),
        "passed": not failures,
        "no_recommendation": no_recommendation,
        "vacuous_assertion": bool(vacuous),
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


def split_enabled(scenarios: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 `enabled` 拆成「可跑」与「未启用」两堆。

    ## 为什么必须拆

    题库里 `enabled: false` 的场景是**依赖尚未落地的能力**（如跨会话记忆）。
    框架若照跑，这些场景必然红 → 「通过率」被未落地的能力拉低，
    看报告的人会误判「功能坏了」；更糟的是有人会去**改断言**让它变绿。

    所以默认**只跑 enabled 的**，未启用的单独报数（不计入分母）。
    要强行跑（看它到底为什么红）用 `--include-disabled`。
    """
    enabled = [s for s in scenarios if s.get("enabled", True)]
    disabled = [s for s in scenarios if not s.get("enabled", True)]
    return enabled, disabled


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
        elif res.get("vacuous_assertion"):
            label = "INVALID(被断言的车从未被推荐过→断言恒真)"
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
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="连 enabled=false 的场景一起跑（默认只跑 enabled 的）",
    )
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    if not scenarios:
        print(f"没有场景可跑（{args.scenarios}）", file=sys.stderr)
        return 2
    if args.scenario:
        scenarios = [s for s in scenarios if s.get("scenario") == args.scenario]
    enabled, disabled = split_enabled(scenarios)
    if args.include_disabled:
        enabled = scenarios
    if args.limit:
        enabled = enabled[: args.limit]

    session = get_session_factory()()
    try:
        results, by_kind = asyncio.run(_run_all(session, enabled))
    finally:
        session.close()

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    print(f"多轮场景：{passed}/{total} 通过")
    for k in sorted(by_kind):
        print(f"  {k}: {by_kind[k]}")
    if disabled:
        reasons = Counter(
            str(s.get("reason_disabled") or "未标注原因") for s in disabled
        )
        print(f"  跳过未启用场景 {len(disabled)} 个（不计入分母）：")
        for reason, n in reasons.most_common():
            print(f"    - {reason}：{n}")

    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(
            json.dumps(
                {
                    "total": total,
                    "passed": passed,
                    "skipped_disabled": len(disabled),
                    "by_kind": dict(by_kind),
                    "results": results,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"报告：{rp}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
