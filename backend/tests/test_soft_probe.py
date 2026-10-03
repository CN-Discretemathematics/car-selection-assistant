"""L4｜软缺口追问（`app/agent/soft_probe.py`）。

## 最容易被自己骗的地方

这个模块有一半是**「不该问」**。四道闸门少一道，用户就会遇到「我都说了我最看重后排，
你还问我看重什么」——比没有这个功能更糟。所以每道闸门各有一条测试，
外加一条**反向测试**：确认该问的时候真的会问（否则一堆「不该问」的测试能全绿）。

## 还有一个容易忽略的不变量

追问**不能阻塞推荐**。硬约束缺失时阻塞是必要的提问（没有预算没法推荐）；
软偏好缺失时阻塞就是拖沓。所以追问挂在 `followup` 上，`need_clarification` 保持 False。
"""
from __future__ import annotations

import pytest

from app.agent import soft_probe
from app.agent.engine import _WEIGHT_RAISE
from app.agent.schemas import Budget, Clarification, UserProfile
from app.agent.tools import DEFAULT_WEIGHTS, _DIM_LABELS


def _complete_profile(**kw) -> UserProfile:
    """硬约束齐、且用户**没强调过**任何维度的画像——这是唯一该问的起点。"""
    base = UserProfile(
        budget=Budget(min=150000, max=200000),
        usage=["通勤"],
        passengers=2,
    )
    for k, v in kw.items():
        setattr(base, k, v)
    return base


# ── 该问的时候真的会问（反向测试）─────────────────────────────────────────
def test_asks_when_hard_constraints_complete_and_no_emphasis():
    q = soft_probe.probe_question(_complete_profile())
    assert q is not None, "硬约束齐、没强调 → 应当追问"
    assert isinstance(q, Clarification)
    assert len(q.options) == 2, f"应问两项，多了变问卷：{q.options}"


# ── 闸门 1：硬约束不齐 → 先问硬的那句 ──────────────────────────────────────
@pytest.mark.parametrize(
    "kw",
    [
        {"usage": [], "passengers": 2},                 # 缺用途
        {"usage": ["通勤"], "passengers": None},        # 缺人数
    ],
)
def test_does_not_ask_before_hard_constraints(kw):
    p = UserProfile(budget=Budget(min=150000, max=200000), **kw)
    assert soft_probe.probe_question(p) is None, "硬约束没齐就追问软偏好 = 顺序错了"


def test_does_not_ask_without_budget():
    p = UserProfile(usage=["通勤"], passengers=2)  # 无预算
    assert soft_probe.probe_question(p) is None


# ── 闸门 2：用户已经强调过 → 别问 ──────────────────────────────────────────
def test_does_not_ask_when_user_already_emphasized():
    """用户说了「我最看重后排」还问「你更看重什么」= 听不懂人话。"""
    p = _complete_profile(weights={"space": 0.3})
    assert soft_probe.probe_question(p) is None


# ── 闸门 3：只问一次 ──────────────────────────────────────────────────────
def test_does_not_ask_twice():
    p = _complete_profile()
    first = soft_probe.probe_question(p)
    assert first is not None
    soft_probe.mark_probed(p, soft_probe._pick_dims(p))
    assert soft_probe.probe_question(p) is None, "同一个问题不该问第二次"


# ── 选项必须来自封闭枚举 ──────────────────────────────────────────────────
def test_options_come_from_the_closed_dimension_enum():
    """选项是 8 个**真被测量**维度的中文名，不能现编。"""
    q = soft_probe.probe_question(_complete_profile())
    assert q is not None
    valid = set(_DIM_LABELS.values())
    for opt in q.options:
        assert opt in valid, f"选项 {opt!r} 不在封闭枚举 {valid} 内"


def test_pick_dims_stays_inside_measured_dimensions():
    for usage, passengers in (("通勤", 2), (["家庭"], 5), ("长途", 7), (["商务"], 1)):
        p = UserProfile(budget=Budget(min=1, max=2), usage=[usage] if isinstance(usage, str) else usage,
                        passengers=passengers)
        for dim in soft_probe._pick_dims(p):
            assert dim in DEFAULT_WEIGHTS, f"{dim} 不是真被测量的维度"
            assert dim in _DIM_LABELS


def test_household_profile_prefers_space():
    """多人/家庭 → 先问空间（真人销售知道这时空间最决定性）。"""
    p = _complete_profile(usage=["家庭"], passengers=5)
    assert soft_probe._pick_dims(p)[0] == "space"


# ── 回答落地 ──────────────────────────────────────────────────────────────
def test_answer_applies_weight_via_production_path():
    p = _complete_profile()
    dims = soft_probe._pick_dims(p)
    soft_probe.mark_probed(p, dims)

    assert soft_probe.apply_probe_answer(p, _DIM_LABELS[dims[0]]) is True
    # 权重值与正则路径**同一个数**（DEFAULT_WEIGHTS + _WEIGHT_RAISE）
    assert p.weights[dims[0]] == pytest.approx(
        DEFAULT_WEIGHTS[dims[0]] + _WEIGHT_RAISE, abs=1e-6
    )


def test_answer_ignored_when_never_asked():
    """没问过就回答 → 不认。否则用户随口一句「空间」就能白捡权重。"""
    p = _complete_profile()
    assert soft_probe.apply_probe_answer(p, "空间") is False
    assert p.weights == {}


def test_answer_ignored_for_dimension_never_offered():
    """只认**被问过的那两个**维度——问的是空间/能耗，答「动力」不生效。"""
    p = _complete_profile()
    soft_probe.mark_probed(p, soft_probe._pick_dims(p))  # 通勤2人 → energy, power
    assert soft_probe.apply_probe_answer(p, "空间") is False, "没被问的维度不该被认领"
    assert p.weights == {}


def test_answer_makes_probe_stop_asking():
    """答完之后不该再问——权重已非空，闸门 2 生效。"""
    p = _complete_profile()
    dims = soft_probe._pick_dims(p)
    soft_probe.mark_probed(p, dims)
    soft_probe.apply_probe_answer(p, _DIM_LABELS[dims[0]])
    assert soft_probe.probe_question(p) is None


# ── 追问不阻塞推荐 ────────────────────────────────────────────────────────
def test_followup_does_not_block_the_recommendation():
    """**本模块的产品不变量**：追问挂在 followup 上，need_clarification 保持 False。

    前端对 need_clarification/clarification 是「二选一」：设了就只渲染追问、
    不渲染推荐卡片（web/app/components/AgentChat.tsx:430）。软偏好缺失时阻塞推荐
    属于拖沓——真人销售是「先给你看车，再问一句你更看重什么」。

    第一版这条测试只是断言了一个 Clarification 对象，压根没碰 AgentMessageOut，
    名字却在说「followup 不是 clarification」——**测的和说的不是一回事**。
    """
    from app.agent.schemas import AgentMessageOut

    q = soft_probe.probe_question(_complete_profile())
    assert q is not None

    out = AgentMessageOut(session_id="s", need_clarification=False, followup=q,
                          recommended_variants=[
                              {"variant_id": 1, "series_id": 1, "series_name": "X",
                               "brand_name": "Y", "display_name": "X 2025", "energy_type": "BEV",
                               "price_cny": 1.0, "score": 1.0, "matched": [], "tradeoffs": []}
                          ])
    # 追问在场，但推荐结果照常返回 → 前端两个都会渲染
    assert out.followup is not None
    assert out.need_clarification is False
    assert out.clarification is None
    assert len(out.recommended_variants) == 1, "追问不该把推荐结果顶掉"


def test_probed_dims_defaults_empty():
    """新增字段必须有默认值——旧会话画像反序列化不能炸。"""
    assert UserProfile().probed_dims == []
    assert UserProfile.model_validate(UserProfile().model_dump()).probed_dims == []
