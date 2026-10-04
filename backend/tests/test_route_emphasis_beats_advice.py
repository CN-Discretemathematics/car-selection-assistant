"""「可执行排序偏好」必须否决两个咨询分支，但**点名车系时不得否决**。

## 生产事故

| 话术 | 修前 | 修后 |
| --- | --- | --- |
| 我最看重动力，**有哪些车推荐** | `tool_loop` → 0 张卡片 | `recommendation` |
| 我最看重动力，**怎么选** | `general_advice` → 0 张卡片 | `recommendation` |

「有哪些 / 对比 / 怎么选」是**问法**，旧判定没区分「要对比」与「要你替我挑」。

## 判据为什么是「hints 里有没有 weights」而不是「句子里有没有强调词」

前两版都用一份**自建**强调词表否决，出了两个问题：

1. 词表与 `engine._EMPHASIS_RE` 两份，靠注释约束同步 → 会漂移；
2. 「我最看重**安全**」这类**算不出权重**的说法也会命中否决，被推去推荐链，
   而 8 维里根本没有安全——用户首要诉求被**静默丢弃**。

`extract_hints` 已经算好 `hints["weights"]`（强调词 ∧ 命中 `_WEIGHT_DIM_KEYWORDS`），
即「这一轮真的产生了可执行偏好」。直接用它：**零第二份词表**，且算不出权重的强调
自动不否决。

## 两条用真实用例钉住的边界

1. **点名车系时不得否决**（第二版 subagent 审查评为高危）：已锁定两款车后问
   「我最看重动力，对比一下这两款」，对比才是对的；第一版把它改成了全库重排，
   等于丢掉用户明确点名的对象。
2. **测试必须复刻生产顺序**：`engine.respond` 是**先** `merge_profile` **再**
   `decide_route`（engine.py:1029 / :1146）。传空 `UserProfile()` 是生产不存在的
   状态，会让「带预算」的用例显得有判别力、实际没有。
"""
from __future__ import annotations

import pytest

from app.agent import routing
from app.agent.engine import extract_hints, merge_profile
from app.agent.routing import decide_route
from app.agent.schemas import UserProfile


def _route(
    message: str,
    resolved: list | None = None,
    profile: UserProfile | None = None,
    db=None,
):
    hints = extract_hints(message)
    p = profile if profile is not None else merge_profile(UserProfile(), hints)
    return decide_route(message, hints, bool(hints), p, resolved or [], db).intent


def _route_without_veto(message: str, resolved: list | None = None) -> str:
    """把否决临时失效（等价于「拆掉修复」）。"""
    saved = routing._ranking_intent_skips_consultation
    routing._ranking_intent_skips_consultation = lambda *a, **k: False
    try:
        return _route(message, resolved)
    finally:
        routing._ranking_intent_skips_consultation = saved


# ── 能证伪修复的用例 ──────────────────────────────────────────────────────
DISCRIMINATING = [
    "我最看重动力，有哪些车推荐",
    "我最看重空间，有哪些SUV推荐",
    "我最看重动力，对比一下这两款",
    "我最看重空间，对比一下轿车和SUV",
    "我最看重动力，怎么选",
    "我比较看重油耗，对比一下两种做法",
    "我最在意续航，对比一下两种方案",
]


@pytest.mark.parametrize("message", DISCRIMINATING)
def test_executable_ranking_intent_beats_consultation_branches(message):
    intent = _route(message)
    assert intent == "recommendation", f"「{message}」被判成 {intent} —— 拿不到推荐卡片"


@pytest.mark.parametrize("message", DISCRIMINATING)
def test_these_cases_actually_discriminate(message):
    """**证明这组用例测得到修复点**：拆掉否决后必须变。

    没有这一条，一组恒绿的测试看起来一样绿，却什么也证明不了
    （第一版就是这样：带预算的用例在生产上本来就该过）。
    """
    before = _route_without_veto(message)
    assert before != "recommendation", (
        f"「{message}」拆掉否决后仍是 recommendation —— 这条用例**测不到**修复点"
    )


# ── 边界①：点名车系时**不得**否决（对比才是对的）─────────────────────────
def test_named_series_comparison_must_not_be_hijacked():
    """已锁定两款车 + 「我最看重动力，对比一下这两款」→ 仍走对比，不进全库重排。

    第一版没看这一条，把差异分析改成了全库重排——丢掉用户明确点名的对象
    （第二版 subagent 审查评为高危）。`_route` 之前恒传 resolved=[]，测不到。
    """
    from app.common.models import VehicleSeries

    fake = [VehicleSeries(id=1, name="A"), VehicleSeries(id=2, name="B")]
    intent = _route("我最看重动力，对比一下这两款", resolved=fake)
    assert intent != "recommendation", (
        f"用户已点名两款车却被推去全库重排（intent={intent}）——对比才是对的"
    )


def test_locked_series_also_blocks_the_veto():
    """画像里锁了车系时同样不得否决（会话内锁定是另一条来源）。"""
    profile = UserProfile(locked_series_ids=[1, 2])
    intent = _route("我最看重动力，有哪些车推荐", profile=profile)
    assert intent != "recommendation", f"已锁定车系却被推去全库重排（intent={intent}）"


# ── 边界②：算不出权重的强调**不得**被推去推荐（安全无对应维度）─────────────
def test_uncomputable_emphasis_is_not_hijacked(db_session):
    """「我最看重安全」：安全**刻意不映射**到任何维度（8 维公式不读安全 fact_key）。

    若用「句子里有强调词」做否决，它会被推去推荐链，而排序里安全权重为 0——
    用户首要诉求被静默丢弃。用 `hints["weights"]` 做判据则自动避开。

    （这句会走到 `mentions_known_brand` 查库，故需要真实 db_session。）
    """
    from app.agent.engine import _WEIGHT_DIM_KEYWORDS

    assert not any("安全" in kw for kw, _ in _WEIGHT_DIM_KEYWORDS), "前提变了：安全已可映射"
    hints = extract_hints("我最看重安全")
    assert not hints.get("weights"), f"前提变了：安全算出了权重 {hints.get('weights')}"
    intent = _route("我最看重安全", db=db_session)
    assert intent != "recommendation", (
        f"算不出权重的强调被推去了推荐链（intent={intent}）——用户首要诉求会被静默丢弃"
    )


# ── 回归护栏：带硬约束 + 排序偏好 ─────────────────────────────────────────
@pytest.mark.parametrize(
    "message",
    ["我比较看重动力，预算15万", "我最看重动力，预算15万", "动力优先，预算25万"],
)
def test_ranking_intent_with_budget_still_recommends(message):
    assert _route(message) == "recommendation", f"「{message}」被判成 {_route(message)}"


# ── 纯咨询必须**仍然**能走咨询分支（别把门关死）──────────────────────────
# ⚠️ 只能放**不含车型/能源词**的问句：生产里 `merge_profile` 会先把句中的
# SUV/MPV/增程 写进画像 → `profile_has_core_constraints` 为 True →
# 「SUV和MPV怎么选」修前修后都是 recommendation。那是既有设计（另一个问题）。
@pytest.mark.parametrize(
    "message",
    ["油车和电车哪个好", "重点说说这两款差别", "这两台车到底有什么区别"],
)
def test_plain_consultation_questions_are_not_hijacked(message):
    intent = _route(message)
    assert intent in ("general_advice", "tool_loop"), (
        f"「{message}」被判成 {intent} —— 纯咨询被误当成购车请求了"
    )


def test_general_advice_branch_is_still_reachable():
    """**必须有一条真正落到 general_advice 的句子**，否则规则 2 是否还活着无从证明。

    第二版 subagent 审查指出：上一组「别把门关死」的用例全被更前面的 0.75
    tool_loop 接走，对规则 2 零覆盖——把规则 2 的否决拆掉它照样绿。
    """
    landed = [
        m for m in ("增程和插混哪个更适合长途", "混动和纯电各有啥优势", "轿车和MPV各有什么好处")
        if _route(m) == "general_advice"
    ]
    assert landed, (
        "没有任何一条用例真正落到 general_advice —— 规则 2 成了不可达分支，"
        "它的否决与否无法被测试区分"
    )


# ── 无第二份词表（第三版已删除自建词表，这里钉住不再回来）──────────────
def test_no_second_emphasis_word_list_exists():
    """否决判据必须读 `hints["weights"]`，不得再引入自建强调词表。

    前两版都因为「另抄一份 `_EMPHASIS_RE` 变体」而被审查点名（词表漂移、
    宽词误伤、安全空转）。这里从结构上钉住：routing 不该再有强调词正则常量。
    """
    words = [n for n in dir(routing) if "EMPHASIS" in n.upper()]
    assert not words, f"routing 又出现了强调词相关常量 {words} —— 判据应只用 hints['weights']"


# ── LLM 执行前置校验必须与 decide_route 同源 ───────────────────────────────
def test_llm_executable_cannot_reintroduce_the_defect():
    """每道门配一句**真的像那扇门**的话，否则断言会因为「本来就不是那个 intent」而绿。

    反向验证 M4 实测的教训：原先 general_advice 的断言挂在
    「我最看重动力，**有哪些车推荐**」上——那句**不像咨询问句**，
    `asks_general_advice` 本来就是 False，于是把那道否决从代码里删掉，
    26 条用例**照样全绿**。等于这道门根本没被测过。
    """
    cases = {
        "tool_loop": "我最看重动力，有哪些车推荐",
        "general_advice": "我最看重动力，怎么选",
    }
    for intent, msg in cases.items():
        hints = extract_hints(msg)
        profile = merge_profile(UserProfile(), hints)
        assert routing.llm_intent_executable(
            intent, msg, hints, bool(hints), profile, [], None
        ) is False, f"{intent} 执行前置没跟上否决——LLM 可把缺陷从这扇门放回来"

    plain = "对比一下这两款车"
    assert routing.llm_intent_executable(
        "tool_loop", plain, extract_hints(plain), False, UserProfile(), [], None
    ) is True, "无排序偏好的对比问句被误拒了 —— 门关过头"


def test_general_advice_gate_is_rejected_for_the_right_reason():
    """general_advice 那道否决必须**自己**在否决，而不是靠别的条件顺带返回 False。

    这是上一条的反向对照：把 `_ranking_intent_skips_consultation` 临时打成
    `False`（等价于拆掉修复），`llm_intent_executable` **必须**翻成 True。
    若仍是 False，说明它的 False 与这道否决无关——门其实没被测到。
    """
    msg = "我最看重动力，怎么选"
    hints = extract_hints(msg)
    profile = merge_profile(UserProfile(), hints)

    assert routing.llm_intent_executable(
        "general_advice", msg, hints, bool(hints), profile, [], None
    ) is False

    saved = routing._ranking_intent_skips_consultation
    routing._ranking_intent_skips_consultation = lambda *a, **k: False
    try:
        without = routing.llm_intent_executable(
            "general_advice", msg, hints, bool(hints), profile, [], None
        )
    finally:
        routing._ranking_intent_skips_consultation = saved

    assert without is True, (
        "拆掉 general_advice 的否决后仍然返回 False —— 该断言与这道门无关，"
        "把它从代码里删掉测试也不会红"
    )
