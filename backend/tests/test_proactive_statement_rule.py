"""「主动说」的三处机制与计划 §1.2 那条判据的关系（2026-10-21 实测）。

## 计划怎么说的

- §1.2 的判据：「当前轮存在**用户已知但本轮未复述**的关键状态，且说出来能改变用户的选择」。
- §1.3 列了三处现成机制（`_priority_phrase` / `_unlock_note` / `next_clarification`），
  并说「三者的共同结构即上表『主动说的判据』」。

## 实测结论：**三者的触发前提各不相同**，且都与那条判据**不同**

| 机制 | 实际触发前提 | 与 §1.2 判据 |
|---|---|---|
| `_priority_phrase` | 回执**本轮刚说过**的约束 | 前提**相反**（判据说「本轮未复述」） |
| `_unlock_note` | 锁定车系与**本轮新需求**冲突 ⇒ 解释范围为什么变了 | 前提**相反**（本轮新出现的状态） |
| `next_clarification` | 追问**尚不存在**的缺失项 | 前提**相反**（缺的是还没有的东西） |

⇒ §1.3 那句「共同结构即主动说的判据」**不准确** ✗（已复核确认 ✓）。

**真正符合 §1.2 判据的是另一个机制**：**跨会话记忆回填后的回执** ——
上次说过的预算这次**已知**、而用户**本轮没复述** ⇒ 说出来（「已记住：预算 20 万」）
让用户知道它被用上了、且**不必再说一遍** ✓。

⇒ 本文件把这件事**钉住** ✓：
- 记忆回填后，追问的回执里**必须**出现那个预算 ✓（= 判据在代码里生效 ✓）；
- 三处机制的前提**各自独立** ✓（谁被「统一」成另一个，哪条就会坏 ✓）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.orm import Session

from app.agent.engine import (
    AgentEngine,
    _priority_phrase,
    _profile_summary,
    next_clarification,
)
from app.agent.preferences import MODE_ENV, encode_value, get_or_create_identity, remember
from app.agent.session import SessionStore
from app.common.llm import LLMClient


@pytest.fixture()
def durable_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV, "on")


def _engine() -> AgentEngine:
    return AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())


# ── 计划 §1.2 那条判据的落点：记忆回填后的回执 ───────────────────────────


def test_restored_memory_is_restated_in_the_clarification(
    db_session: Session, durable_on: None
):
    """上次说过的预算，这次**已知**而用户**本轮没复述** ⇒ 追问时说出来 ✓。

    这正是计划 §1.2 那条判据：「当前轮存在『用户已知但本轮未复述』的关键状态，
    且说出来能改变用户的选择」——说出来，用户知道它被用上了、且不必再说一遍 ✓。
    """
    identity, _token, _created = get_or_create_identity(db_session)
    db_session.commit()
    remember(
        db_session,
        identity,
        category="constraint",
        key="budget",
        value=encode_value("budget", {"min": 200000.0, "max": 250000.0}),
        source_type="explicit",
        confirmed=True,
    )
    db_session.commit()

    engine = _engine()
    # **新会话**：用户只说了用途，预算是**记忆带回来的**
    sid = engine._store.create()
    out = asyncio.run(engine.handle(db_session, sid, "平时上下班通勤", identity=identity))

    # 追问文案在 `clarification.question` 里（`explanation` 为空 ✓）
    question = out.clarification.question if out.clarification else ""
    assert "预算" in question, f"回执里应复述记忆带来的预算：{question!r}"
    assert "20" in question, question
    # 只问**还缺**的那一项（预算已由记忆补上 ⇒ 该问人数 ✓）
    assert out.clarification is not None and out.clarification.missing == ["passengers"], (
        out.clarification
    )


def test_profile_summary_lists_what_is_known():
    """`_profile_summary` 只复述**画像里已有**的（不猜不补）✓。"""
    from app.agent.schemas import UserProfile

    assert _profile_summary(UserProfile()) == ""
    profile = UserProfile(
        budget={"min": 200000.0, "max": 250000.0, "currency": "CNY", "type": "official_msrp"},
        usage=["通勤"],
    )
    summary = _profile_summary(profile)
    assert "预算" in summary and "通勤" in summary, summary


# ── 三处机制的前提**各不相同**（钉住「不要被统一」）─────────────────────


def test_priority_phrase_echoes_what_was_just_said():
    """`_priority_phrase` 回执的是**画像里已有**的（用户刚说过的）✓。

    ⚠️ 它的前提与 §1.2 那条判据**相反**（判据说「本轮未复述」）⇒
    把它当成「主动说已知但未复述的东西」会**说错话** ✓。
    """
    from app.agent.schemas import UserProfile

    profile = UserProfile(weights={"power": 0.8}, usage=["通勤"])
    phrase = _priority_phrase(profile)
    assert phrase, "有侧重维度时应有回执"
    assert "动力" in phrase, phrase
    # 画像里没有的东西**不会被说出来**（不猜不补 ✓）
    assert "预算" not in phrase, phrase


def test_clarification_asks_only_the_missing_one():
    """`next_clarification` **一次只问一项**（最小化追问）✓。

    它的前提是「还**缺**一项」——与 §1.2 判据（已知但未复述）**相反** ✓。
    """
    from app.agent.schemas import UserProfile

    # 缺预算、有用途 ⇒ 只问预算
    only_budget = next_clarification(UserProfile(usage=["通勤"]))
    assert only_budget is not None
    assert only_budget.missing == ["budget"], only_budget

    # 预算与用途都有 ⇒ 不该再问预算
    both = next_clarification(
        UserProfile(
            budget={"min": 200000.0, "max": 250000.0, "currency": "CNY", "type": "official_msrp"},
            usage=["通勤"],
        )
    )
    assert both is None or "budget" not in both.missing, both


def test_unlock_note_only_explains_a_scope_change(db_session: Session):
    """`_unlock_note` 只在**锁定车系被解除**时出现 ⇒ 解释「范围为什么变了」✓。

    它的前提是「**本轮新出现**的冲突」——与 §1.2 判据（已知但未复述）**相反** ✓。
    """
    from tests.seed import make_brand, make_series, make_source

    source = make_source(db_session, name="解锁说明来源")
    brand = make_brand(db_session, name="解锁说明品牌", source=source)
    series = make_series(
        db_session, brand, name="被锁车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    db_session.commit()

    note = AgentEngine._unlock_note(db_session, [series.id])
    assert "已解除" in note, note
    assert "被锁车系" in note, note
