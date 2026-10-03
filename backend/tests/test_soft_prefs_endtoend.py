"""L1 打开时的端到端硬判据：硬约束满足率不得回归（sales-agent-proposal §5 判据 1）。

## 这条测试在守什么

提案 §5 的第一条硬判据是「硬约束满足率不得回归——现有 valid-hit@5 /
valid-precision@5 / valid-MRR 逐位不变」。**默认模式下 L1 是关的**，所以这条判据
平时根本不会被触发：关着的功能不会弄坏东西，一句话就能通过。

真正的风险出现在**有人把 `AGENT_SOFT_PREF_MODE` 切到 `llm` 的那一刻**。而 L1 唯一的
落画像通道 `household_size → passengers` 会**下推成座位数过滤**——也就是说：

> L1 一旦猜错家庭规模，就会静默砍掉候选集，且没有任何东西会报错。

这正是本仓反复吃亏的那类缺陷（座位数下推的 `IS NULL`、画像脏数据恒 500）。所以这里
把它钉成常驻测试，而不是留给「接入排序那一轮再说」。

## 三条不变量

1. **只改权重、不改候选集**：usage / pain_points / priority_order 全是软信号，
   落画像后不得让任何一款满足用户已述硬约束的车消失或出现。
2. **不能凭空造硬约束**：即便模型自作聪明地想给 body_type / energy_preference，
   `sanitize` 也必须把它丢掉——猜错车身的代价是砍掉整个轿车候选集。
3. **缺座位数据 ≠ 不满足**：L1 推出来的 passengers 不能把「没有座位事实」的款型
   一起筛掉（6629 款型里 85% 属于这一类）。

全部用假 LLM 驱动，**不发起任何真实网络调用**。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy.orm import Session

from app.agent import soft_prefs as sp
from app.agent.schemas import Budget, UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


class _FakeLLM:
    """按脚本返回固定 JSON 的假客户端。"""

    available = True

    def __init__(self, payload: dict | str | None):
        self.content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.calls: list[dict] = []

    async def chat(self, msgs, tools=None, temperature=0.2, json_mode=False, thinking=None):
        self.calls.append({"json_mode": json_mode, "thinking": thinking})
        return {"choices": [{"message": {"content": self.content}}]}


# ── 种子：一台「符合全部硬约束」的车 + 两台各违反一条的对照车 ──────────────────
@pytest.fixture()
def seeded(db_session: Session) -> dict[str, int]:
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    out: dict[str, int] = {}
    # 目标车：SUV + BEV + 15 万 + 5 座 + 车长 4600（唯一能靠「空间」翻盘的对手）
    plan = [
        ("合规车", "suv", "BEV", "150000", ("参数信息", "座位数(个)", "5", "个", None)),
        ("轿车对照", "sedan", "BEV", "150000", ("参数信息", "座位数(个)", "5", "个", None)),
        ("燃油对照", "suv", "ICE", "150000", ("参数信息", "座位数(个)", "5", "个", None)),
        ("超预算对照", "suv", "BEV", "250000", ("参数信息", "座位数(个)", "5", "个", None)),
        ("无座位事实", "suv", "BEV", "150000", None),
    ]
    for name, body, energy, price, fact in plan:
        s = make_series(db_session, brand, name=name, body_type=body, energy_types=(energy,), source=source)
        y = make_year(db_session, s)
        facts = [fact] if fact else []
        out[name] = make_variant(
            db_session, s, y, price_cny=price, energy_type=energy, facts=facts, source=source
        ).id
    db_session.commit()
    return out


def _profile() -> UserProfile:
    """用户已明确说出的硬约束：预算 / 车身 / 能源。"""
    return UserProfile(
        budget=Budget(min=100000, max=200000),
        body_type=["suv"],
        energy_preference=["BEV"],
    )


def _ids(db: Session, profile: UserProfile) -> list[int]:
    res = recommendation_tool(db, profile, limit=50)
    return [v["variant_id"] for v in res["variants"]]


# ── 不变量 1：只改权重、不改候选集 ───────────────────────────────────────────
def test_soft_prefs_change_weights_but_not_the_candidate_set(db_session: Session, seeded, monkeypatch):
    """L1 只允许改变**排序依据**，不允许改变**候选集合**。

    候选集变了就等于硬约束被悄悄改写——而用户并没有说过任何新的硬约束。
    """
    monkeypatch.setenv(sp.MODE_ENV, "llm")
    message = "平时通勤，最看重空间"
    llm = _FakeLLM(
        {
            "usage_scenario": {"value": "通勤", "evidence": "平时通勤"},
            "pain_points": [{"value": "空间尺寸", "evidence": "空间"}],
            "priority_order": [{"value": "space", "evidence": "最看重空间"}],
        }
    )

    off_profile = _profile()
    baseline = set(_ids(db_session, off_profile))

    on_profile = _profile()
    assert asyncio.run(sp.run_if_enabled(on_profile, message, llm=llm)) is True

    # 非空断言：L1 确实改了权重，否则上面那条「候选集不变」就是空话
    assert on_profile.weights.get("space") is not None
    assert off_profile.weights == {}

    assert set(_ids(db_session, on_profile)) == baseline, "L1 改变了候选集——硬约束被改写了"


# ── 不变量 2：不能凭空造硬约束 ─────────────────────────────────────────────
def _budget_only() -> UserProfile:
    """用户**只**说了预算，没提车身/能源——正是硬约束可以被凭空发明的场景。"""
    return UserProfile(budget=Budget(min=100000, max=200000))


def test_soft_prefs_cannot_invent_body_constraint(db_session: Session, seeded, monkeypatch):
    """用户没说车身时，LLM 猜一个 SUV 也不许生效。

    「周末偶尔带孩子」被猜成 SUV，会砍掉整个轿车候选集——用户从没要求过。
    这与本仓已拒绝的「把『我最看重安全』映射到某维度」是同一类错误：用行为冒充理解。

    前提断言很重要：若用户本就说了 SUV，LLM 再猜 SUV 是**看不出来的**，
    那样的测试等于没测。
    """
    monkeypatch.setenv(sp.MODE_ENV, "llm")
    message = "周末偶尔带孩子出去玩"
    llm = _FakeLLM(
        {
            # body_type / energy_type 不在 L1 的 schema 里，必须原样丢弃
            "body_type": {"value": "suv", "evidence": "带孩子出去玩"},
            "energy_type": {"value": "BEV", "evidence": "出去玩"},
            "household_size": {"value": "3~5人", "evidence": "带孩子"},
        }
    )

    profile = _budget_only()
    baseline = set(_ids(db_session, profile))
    assert seeded["轿车对照"] in baseline, "前提：没约束车身时轿车本来就在候选里"

    assert asyncio.run(sp.run_if_enabled(profile, message, llm=llm)) is True
    assert profile.body_type == [], "LLM 凭空造出了车身硬约束"
    assert set(_ids(db_session, profile)) == baseline, "凭空的车身约束砍掉了候选"


def test_soft_prefs_cannot_invent_energy_constraint(db_session: Session, seeded, monkeypatch):
    """凭空造能源同样禁止——否则燃油/纯电里有一边会被静默筛空。

    注意这里与车身那条的**预期返回值不同**：本例 LLM 的输出**全部**落在 schema 之外，
    `sanitize` 一个字段都留不下 → 整条丢弃 → `run_if_enabled` 返回 False、画像零改动。
    这比「部分保留」更严格，也顺带覆盖了提案 §5 要求的「越界偏好必须拒绝并退回正则路径」。
    """
    monkeypatch.setenv(sp.MODE_ENV, "llm")
    message = "平时通勤就好"
    llm = _FakeLLM({"energy_type": {"value": "ICE", "evidence": "平时通勤就好"}})

    profile = _budget_only()
    baseline = set(_ids(db_session, profile))
    assert seeded["燃油对照"] in baseline and seeded["合规车"] in baseline
    before = profile.model_dump()

    # 全部字段越界 → 整条作废，不落任何画像
    assert asyncio.run(sp.run_if_enabled(profile, message, llm=llm)) is False
    assert profile.model_dump() == before, "越界输出改动了画像"
    assert set(_ids(db_session, profile)) == baseline


def test_soft_prefs_dropped_fields_leave_profile_untouched(db_session: Session, monkeypatch):
    """只给 body_type 的输出，画像应当**一点没动**（连 weights 都不该有）。"""
    monkeypatch.setenv(sp.MODE_ENV, "llm")
    message = "就想要台车"
    llm = _FakeLLM({"body_type": {"value": "sedan", "evidence": "就想要台车"}})
    profile = _profile()
    before = profile.model_dump()
    assert asyncio.run(sp.run_if_enabled(profile, message, llm=llm)) is False
    assert profile.model_dump() == before


# ── 不变量 3：缺座位数据 ≠ 不满足 ───────────────────────────────────────────
def test_soft_prefs_household_keeps_variants_without_seat_facts(db_session: Session, seeded, monkeypatch):
    """L1 推出 passengers 后，**没有座位事实**的款型必须仍然保留。

    passengers 会下推成 `seat_count IS NULL OR seat_count >= N`。若 L1 的座位数
    判定被改成纯 `>= N`，本地快照库 6629 款型里 85%（没有座位事实）会被静默筛空。
    """
    monkeypatch.setenv(sp.MODE_ENV, "llm")
    message = "我们一家五口人"
    llm = _FakeLLM({"household_size": {"value": "5人以上", "evidence": "一家五口人"}})

    profile = _profile()
    assert asyncio.run(sp.run_if_enabled(profile, message, llm=llm)) is True
    assert profile.passengers == 5, "「5人以上」必须按下界 5 解析（够用即可，不得按 7 座砍车）"

    ids = set(_ids(db_session, profile))
    assert seeded["无座位事实"] in ids, "没有座位事实的款型被误杀了——缺数据 ≠ 不满足"
    assert seeded["合规车"] in ids, "5 座合规车应当保留"


def test_soft_prefs_never_override_a_user_stated_household(db_session: Session, seeded, monkeypatch):
    """用户已经明说的人数，LLM 不得改写（regex 永远压过 LLM）。"""
    monkeypatch.setenv(sp.MODE_ENV, "llm")
    message = "平时3个人乘坐，偶尔人多"
    llm = _FakeLLM({"household_size": {"value": "5人以上", "evidence": "人多"}})

    profile = _profile()
    profile.passengers = 3          # 相当于本轮 extract_hints 已从正则拿到 3
    assert asyncio.run(sp.run_if_enabled(profile, message, llm=llm)) is False
    assert profile.passengers == 3, "LLM 覆盖了用户已明说的人数"


# ── 模式闸门在端到端口径下同样成立 ─────────────────────────────────────────
def test_soft_prefs_off_is_byte_identical(db_session: Session, seeded, monkeypatch):
    """默认 off：画像逐字段不变，候选集逐个不变，且**一次 LLM 都不发**。"""
    monkeypatch.setenv(sp.MODE_ENV, "off")
    message = "平时通勤，最看重空间，家里五口人"
    llm = _FakeLLM(
        {
            "usage_scenario": {"value": "家庭", "evidence": "家里五口人"},
            "household_size": {"value": "5人以上", "evidence": "家里五口人"},
            "priority_order": [{"value": "space", "evidence": "最看重空间"}],
        }
    )
    profile = _profile()
    before = profile.model_dump()
    ids_before = _ids(db_session, profile)

    assert asyncio.run(sp.run_if_enabled(profile, message, llm=llm)) is False
    assert llm.calls == [], "off 模式不得发起任何 LLM 调用"
    assert profile.model_dump() == before
    assert _ids(db_session, profile) == ids_before
