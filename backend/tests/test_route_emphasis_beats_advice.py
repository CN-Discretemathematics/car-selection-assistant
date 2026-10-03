"""用户表达了偏好/强调时，**不得**走 general_advice（2026-10-04）。

## 实测缺陷

首轮输入「我比较看重动力，预算15万」「我比较看重空间，预算15万」，系统给了一段
聊天的回答，**一张推荐卡片都没有**。而 `20万预算要家用SUV` 却是正常的——差别只在
那两个字「**比较**」。

## 两个叠加的成因

1. `_GENERAL_ADVICE_RE` 里的**裸 `比较`** 命中了「比**较**看重」；
2. 该分支的判据只看**已持久化的画像**（`profile_has_core_constraints`），
   **首轮画像必然为空**，于是即使本条消息里明明白白写了预算，也照样被 general_advice 抢先。

## 为什么用「强调词否决」而不是去改那个正则

「最看重/优先/主要看」是「**你帮我挑**」的最强信号，与「怎么选？」语义正相反。
所以只要用户表达了偏好，就该否决 general_advice——比把 `比较` 改成更刁钻的正则
更稳，也顺带覆盖了 `看重/在乎/重视/重点` 等同族说法。

## 两条必须守住的回归

- 真正的「怎么选」**仍然**走 general_advice（「SUV和MPV怎么选」）；
- 判据与 `decide_route` 规则 2 用**同一份**表达式，否则一致性校验会和真实决策打架。
"""
from __future__ import annotations

import pytest

from app.agent.engine import extract_hints
from app.agent.routing import decide_route
from app.agent.schemas import UserProfile


def _route(message: str) -> str:
    return decide_route(message, extract_hints(message), bool(extract_hints(message)),
                         UserProfile(), [], None).intent


# ── 缺陷本体：带强调词 + 预算，首轮必须出推荐 ───────────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        "我比较看重动力，预算15万",
        "我比较看重空间，预算15万",
        "比较看重续航，预算20万",
        "我最看重动力，预算15万",     # 这条本来就对，钉住别回归
        "看重动力，预算15万",
        "动力优先，预算15万",
        "我最在意油耗，预算15万",
        "空间优先，预算25万",
    ],
)
def test_emphasis_never_routes_to_general_advice(message):
    intent = _route(message)
    assert intent == "recommendation", (
        f"「{message}」被判成 {intent} —— 用户拿不到任何推荐卡片"
    )


@pytest.mark.parametrize(
    "message",
    ["我比较看重动力", "空间优先", "我最看重后排空间"],
)
def test_emphasis_alone_also_routes_to_recommendation(message):
    """哪怕没提预算：说了「我最看重 X」就是要人替他挑。"""
    assert _route(message) == "recommendation", f"「{message}」不该走 general_advice"


# ── 真正的「怎么选」必须**仍然**走 general_advice ──────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        "SUV和MPV怎么选",
        "油车和电车哪个好",
        "增程和插混有什么区别",
        "预算有限怎么挑",
    ],
)
def test_real_comparison_questions_still_reach_general_advice(message):
    intent = _route(message)
    # 有的会被更前面的 tool_loop 分支接走（等价：都不是推荐）
    assert intent in ("general_advice", "tool_loop"), (
        f"「{message}」被判成 {intent} —— 真正的选购咨询被误当成购车请求了"
    )


def test_llm_executable_check_mirrors_the_decision_rule():
    """`llm_intent_executable` 的 general_advice 分支必须与 decide_route 规则 2 同源。

    两处判据不同步的话，LLM 路由层会在日志上说"一致"而实际走了别的分支——
    这类"看起来通过"的偏差比直接报错更难查。

    ⚠️ 用例**必须不含任何硬约束**（预算/用途/人数）：`llm_intent_executable` 里
    `core_constraints` 由 hints 与 profile 推出，只要带预算，`not core_constraints`
    就已经是 False，压根轮不到强调词否决——第一版就栽在这，测试因为错误的理由而全绿。
    这里用「我最看重空间，对比一下SUV和MPV」：有「对比」能命中 general_advice、
    有「看重」该被否决、但零硬约束。
    """
    from app.agent import routing

    msg = "我最看重空间，对比一下SUV和MPV"
    hints = extract_hints(msg)
    assert not ({"budget", "passengers", "usage"} & set(hints)), "用例带了硬约束，测不到强调词"
    assert routing.asks_general_advice(msg), "用例命中不了 general_advice，测不到分支"

    # 强调词否决后，LLM 不得把这句话改写成 general_advice
    # ⚠️ 签名是 (intent, message, ...)——**intent 在前**。第一版按 (message, intent)
    # 传，intent 变成那句中文、落到「未知 intent 一律 False」分支，于是无论有没有
    # 强调词否决都返回 False——测试因为错误的理由而全绿。
    executable = routing.llm_intent_executable(
        "general_advice", msg, hints, bool(hints), UserProfile(), [], None
    )
    assert executable is False, (
        "执行前置校验没跟上强调词否决——LLM 仍可把「我最看重…」改写成 general_advice，"
        "于是同一个缺陷从 LLM 路由层复活"
    )

    # 对照：没有强调词的同类问题，general_advice 仍然可执行（别把门关死）
    plain = "对比一下SUV和MPV"
    assert routing.llm_intent_executable(
        "general_advice", plain, extract_hints(plain), False, UserProfile(), [], None
    ) is True, "无强调词的对比问句被误拒了 —— 门关过头了"
