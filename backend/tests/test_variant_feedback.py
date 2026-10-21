"""拒绝**跨会话记住**（计划 Phase 2b「反馈表：拒绝跨会话记住」）。

## 为什么要有它

会话内拒绝（`profile.rejected_variant_ids`）只在**本次对话**有效 ⇒ 下次会话
又会把用户明确说过「不要」的车推给他。拒绝是**最强的负反馈**：用户说「这台不要」
就是不要；忘了它等于没听见。

## 读回不新造排除机制

反馈读回后**并进 `profile.rejected_variant_ids`** ⇒ 推荐链**完全复用**会话内拒绝
那套排除逻辑（工具层零改动 ✓）⇒ 两种排除的行为必然一致 ✓。

## 三条纪律

1. **只写本轮新增的排除项**（差集）⇒ 重复拒绝不重复写 ✓；
2. **模式闸门**与偏好记忆同一套（`off` 不写不读 / `shadow` 只落日志 / `on` 写+读）✓；
3. **写失败绝不影响**用户这一轮的正常回答 ✓。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.orm import Session

from app.agent.engine import AgentEngine
from app.agent.feedback import load_rejected_variant_ids, record_feedback
from app.agent.preferences import MODE_ENV, get_or_create_identity
from app.agent.session import SessionStore
from app.common.llm import LLMClient
from app.common.models import VariantFeedback
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


@pytest.fixture()
def durable_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开持久化闸门（默认 off，测试必须显式打开）。"""
    monkeypatch.setenv(MODE_ENV, "on")


def _seed_two_cars(db_session: Session) -> tuple[str, str]:
    """种两个车系（各一款），返回 (车系A, 车系B)。"""
    source = make_source(db_session, name="反馈测试来源")
    brand = make_brand(db_session, name="反馈测试品牌", source=source)
    names = []
    for name in ("反馈车系甲", "反馈车系乙"):
        series = make_series(
            db_session, brand, name=name, body_type="sedan",
            energy_types=("BEV",), source=source,
        )
        year = make_year(db_session, series)
        make_variant(
            db_session, series, year, config_version="标准版", energy_type="BEV",
            price_cny="150000", source=source,
        )
        names.append(name)
    db_session.commit()
    return names[0], names[1]


def _identity(db_session: Session):
    identity, _token, created = get_or_create_identity(db_session)
    db_session.commit()
    assert created is True
    return identity


# ── 存储层往返 ──────────────────────────────────────────────────────────────


def test_feedback_round_trip(db_session: Session, durable_on: None):
    """写进去的款型 id 能原样读回来。"""
    _seed_two_cars(db_session)
    identity = _identity(db_session)
    from app.common.models import VehicleVariant

    vid = db_session.query(VehicleVariant.id).order_by(VehicleVariant.id).first().id

    written = record_feedback(db_session, identity, [vid], reason="太贵了")
    assert written == [vid]
    assert load_rejected_variant_ids(db_session, identity) == [vid]


def test_off_mode_writes_nothing(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """`off` 模式不写、不读（行为与接入前逐位相同）。"""
    monkeypatch.setenv(MODE_ENV, "off")
    _seed_two_cars(db_session)
    identity = _identity(db_session)
    from app.common.models import VehicleVariant

    vid = db_session.query(VehicleVariant.id).order_by(VehicleVariant.id).first().id

    assert record_feedback(db_session, identity, [vid], reason="太贵了") == []
    assert load_rejected_variant_ids(db_session, identity) == []
    assert db_session.query(VariantFeedback).count() == 0


def test_repeated_rejection_keeps_one_row(db_session: Session, durable_on: None):
    """同一款型重复拒绝 ⇒ 只留一行（幂等 ✓）。"""
    _seed_two_cars(db_session)
    identity = _identity(db_session)
    from app.common.models import VehicleVariant

    vid = db_session.query(VehicleVariant.id).order_by(VehicleVariant.id).first().id

    record_feedback(db_session, identity, [vid], reason="太贵了")
    record_feedback(db_session, identity, [vid], reason="不想要这个配置")
    rows = db_session.query(VariantFeedback).filter_by(identity_id=identity.id).all()
    assert len(rows) == 1
    assert rows[0].reason == "不想要这个配置"


# ── 端到端：拒绝跨会话生效 ──────────────────────────────────────────────────


def test_rejection_is_remembered_in_a_new_session(db_session: Session, durable_on: None):
    """会话 1 说「甲车系不要」⇒ **新会话**里不再推它。"""
    car_a, _car_b = _seed_two_cars(db_session)
    identity = _identity(db_session)
    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())

    # 会话 1：先拿到推荐，再拒绝其中一个车系
    sid1 = engine._store.create()
    first = asyncio.run(
        engine.handle(db_session, sid1, "预算15万，通勤，两三个人用", identity=identity)
    )
    assert first.recommended_variants, "第一轮该有推荐（前提）"
    reject = asyncio.run(
        engine.handle(db_session, sid1, f"{car_a}不要，太贵了", identity=identity)
    )
    assert reject.recommended_variants, "拒绝后应该还有别的车可推"

    # 反馈表里应有记录
    assert db_session.query(VariantFeedback).filter_by(identity_id=identity.id).count() > 0

    # **新会话**：同一个身份 ⇒ 被拒的车系不再出现
    sid2 = engine._store.create()
    again = asyncio.run(
        engine.handle(db_session, sid2, "预算15万，通勤，两三个人用", identity=identity)
    )
    names = {v.series_name for v in again.recommended_variants or []}
    assert car_a not in names, f"新会话里又推了被拒的车系：{names}"


def test_rejection_is_not_remembered_when_the_gate_is_off(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """**A/B 对照**：模式关掉时，拒绝**不跨会话**记住（行为与接入前逐位相同 ✓）。

    这条是「记住」这个能力的**反证**——只测「记住时生效」的话，
    一个「什么都没做但恰好没推」的实现也能通过 ✓。
    """
    monkeypatch.setenv(MODE_ENV, "off")
    car_a, _car_b = _seed_two_cars(db_session)
    identity = _identity(db_session)
    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())

    sid1 = engine._store.create()
    asyncio.run(
        engine.handle(db_session, sid1, "预算15万，通勤，两三个人用", identity=identity)
    )
    asyncio.run(
        engine.handle(db_session, sid1, f"{car_a}不要，太贵了", identity=identity)
    )
    assert db_session.query(VariantFeedback).count() == 0, "off 模式不该写反馈表"

    sid2 = engine._store.create()
    again = asyncio.run(
        engine.handle(db_session, sid2, "预算15万，通勤，两三个人用", identity=identity)
    )
    names = {v.series_name for v in again.recommended_variants or []}
    assert car_a in names, "off 模式下不该跨会话记住（应照旧推出来）"
