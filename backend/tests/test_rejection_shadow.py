"""拒绝反馈的影子评估（计划 Phase 3「反馈驱动偏好调整（shadow）」）。

## 它在算什么

用户拒绝一台车后，后台算一次**反事实**：「若把这次拒绝上升为**品牌级排除项**
（Phase 2b 反馈表的候选口径），推荐会怎么变、**会误伤几台**」。

**只算不改**（影子）：结论进结构化日志，不进用户回答、不改推荐结果。

## 守卫

- 影子**默认关**（每次要多跑一遍推荐查询，不该默认付）；
- 开着时**回答逐字不变**（影子不得影响用户看到的任何东西）；
- 开着时会落一条结构化日志，且**误伤面**算得对（同品牌其它在榜的车要算进去，
  被拒的那台不算——它本来就被排除了，不算误伤）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from sqlalchemy.orm import Session

import app.agent.engine as engine_mod
from app.agent.engine import AgentEngine
from app.agent.rejection_shadow import brand_exclusion_delta
from app.agent.session import SessionStore
from app.common.llm import LLMClient
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_two_brands(db_session: Session) -> dict[str, int]:
    """同一个品牌的两台车——用来量「品牌级排除会误伤几台」。

    同品牌**必须有两台**：被拒掉一台之后，榜上还得剩一台同品牌的，
    品牌级排除才会真的误伤（否则误伤面恒为 0，量不出东西）。
    ⚠️ 刻意**只用同品牌的两台**：推荐同分时的排序依赖查询顺序，
    掺进别的品牌就会让「被拒的是哪台」变得不确定，断言随之变脆。
    """
    source = make_source(db_session, name="影子测试来源")
    brand = make_brand(db_session, name="甲品牌", source=source)
    ids: dict[str, int] = {}
    for series_name, price in (("甲车系", "150000"), ("甲车系二代", "155000")):
        series = make_series(
            db_session, brand, name=series_name, body_type="sedan",
            energy_types=("BEV",), source=source,
        )
        year = make_year(db_session, series)
        variant = make_variant(
            db_session, series, year, config_version="标准版", energy_type="BEV",
            price_cny=price, source=source,
        )
        ids[series_name] = variant.id
    db_session.commit()
    return ids


def _factory(db_session: Session):
    """把引擎的 session 工厂指向测试库（影子自己开连接，默认连不到夹具的内存库）。"""
    @contextlib.contextmanager
    def _cm():
        yield db_session

    return lambda: _cm


def _settings_with_shadow(on: bool):
    """替代 `get_settings()`：返回一个只有影子开关的设置对象。

    注意是**可调用**的（引擎里写的是 `get_settings().xxx`）。
    """

    class _Stub:
        agent_rejection_shadow_enabled = on

    return lambda: _Stub()


def _drive(engine: AgentEngine, db_session: Session, messages: list[str]) -> None:
    """跑一轮对话，并把旁路任务**等完**（否则事件循环一关，任务就被取消）。"""
    async def _run() -> None:
        sid = engine._store.create()
        for message in messages:
            await engine.respond(db_session, sid, message)
        pending = list(engine._pending_shadow_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(_run())


def test_brand_exclusion_delta_counts_the_collision_surface():
    """误伤面 = 同品牌其它在榜的车（**被拒那台不算**——它本来就被排除）。"""
    before = [
        {"variant_id": 1, "brand_name": "甲品牌"},
        {"variant_id": 2, "brand_name": "甲品牌"},
        {"variant_id": 3, "brand_name": "乙品牌"},
    ]
    after = [{"variant_id": 3, "brand_name": "乙品牌"}]
    delta = brand_exclusion_delta(
        before, after, brand_name="甲品牌", rejected_variant_id=2
    )
    assert delta["same_brand_in_topn"] == 1, delta
    assert delta["left"] == [1, 2], delta
    assert delta["entered"] == [], delta
    assert delta["topn_changed"] is True, delta


def test_shadow_is_disabled_by_default():
    """默认**关**：影子每次要多跑一遍推荐查询（全库扫描级开销）。"""
    from app.common.config import get_settings

    assert get_settings().agent_rejection_shadow_enabled is False


def test_shadow_logs_the_brand_exclusion_delta(db_session, monkeypatch, caplog):
    """开着影子时，拒绝后要落一条结构化日志，且误伤面算得对。"""
    _seed_two_brands(db_session)
    monkeypatch.setattr(engine_mod, "get_session_factory", _factory(db_session))
    monkeypatch.setattr(engine_mod, "get_settings", _settings_with_shadow(True))

    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())
    with caplog.at_level(logging.INFO, logger="app.agent.rejection.shadow"):
        _drive(engine, db_session, ["预算20万，通勤，两三个人", "不喜欢这台"])

    records = [
        json.loads(r.message)
        for r in caplog.records
        if r.name == "app.agent.rejection.shadow" and r.levelno == logging.INFO
    ]
    assert records, "影子评估没有落日志（旁路出错时也不该静默）"
    rec = records[0]
    assert rec["shadow"] == "rejection_brand_exclusion", rec
    assert rec["rejected_variant_id"], rec
    assert rec["brand_name"], rec
    # 被拒车所在品牌的**另一台**也在榜 → 品牌级排除会误伤它
    assert rec["same_brand_in_topn"] >= 1, rec


def test_shadow_does_not_change_the_response(db_session, monkeypatch):
    """影子**只算不改**：开着与关着，用户看到的回答逐字相同。"""
    _seed_two_brands(db_session)
    monkeypatch.setattr(engine_mod, "get_session_factory", _factory(db_session))

    def _answers(shadow_on: bool) -> list[str]:
        monkeypatch.setattr(engine_mod, "get_settings", _settings_with_shadow(shadow_on))
        engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())
        answers: list[str] = []

        async def _run() -> None:
            sid = engine._store.create()
            for message in ("预算20万，通勤，两三个人", "不喜欢这台"):
                out = await engine.respond(db_session, sid, message)
                answers.append("\n".join([out.explanation or "", *out.reasons]))
            pending = list(engine._pending_shadow_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_run())
        return answers

    assert _answers(True) == _answers(False)
