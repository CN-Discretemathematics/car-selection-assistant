"""补行为断言：段一/段二**做了什么**，而不只是它们「在哪个文件」。

第一版的测试只钉结构（符号在不在 turn.py），于是「把 unknowns 清理改成 pass」
这种**行为破坏**照样全绿——结构没变，内容坏了。变异测试 5/6 MISS 暴露了这一点。

本文件用**真实的 db_session + 真实 store** 走一遍段一/段二，断言可观察的行为。
"""
from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from app.agent import turn
from app.agent.schemas import UserProfile
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


class _FakeStore:
    """够用的 SessionStore 替身：只存画像。

    注意：`_load_profile` 用 `run_in_threadpool(store.get_profile, ...)` 调用，
    所以这里的接口必须是**同步**的（threadpool 里跑的是普通函数）。
    """

    def __init__(self) -> None:
        self.profile: dict = {}

    def get_profile(self, session_id: str) -> dict:
        return dict(self.profile)

    def set_profile(self, session_id: str, data: dict) -> None:
        self.profile = dict(data)


# ── 段一：unknowns 清理 ─────────────────────────────────────────────────────


def test_advance_profile_clears_answered_unknowns(db_session: Session):
    """用户补齐预算后，「预算」不应再留在 unknowns 里。

    unknowns 的语义是「最近一次追问未答」——补齐了就得移掉，否则用户补了
    预算却还被继续追问预算（评审 M1）。
    """
    store = _FakeStore()
    store.profile = UserProfile(unknowns=["budget", "usage", "passengers"]).model_dump()

    result = asyncio.run(turn.advance_profile(store, "s1", "预算 20 万左右"))

    assert "budget" not in result.profile.unknowns
    assert "usage" in result.profile.unknowns, "没提到的用途不该被清掉"
    assert "passengers" in result.profile.unknowns


def test_advance_profile_merges_hints(db_session: Session):
    """本轮线索必须并进画像（段一的本职工作）。"""
    store = _FakeStore()
    store.profile = UserProfile().model_dump()

    result = asyncio.run(turn.advance_profile(store, "s1", "预算 20 万，家庭用车"))

    assert result.profile.budget.max == 200000
    assert "家庭" in result.profile.usage
    assert result.structured is True


# ── 段二：品牌约束 + 锁定 ───────────────────────────────────────────────────


def test_advance_series_locks_merges_brand_constraint(db_session: Session):
    """「只要奔驰」必须变成品牌硬约束（写进画像，下推 SQL）。"""
    make_brand(db_session, name="奔驰")
    db_session.commit()
    profile = UserProfile()

    resolved, structured, wants_unlock = asyncio.run(
        turn.advance_series_locks(db_session, "只要奔驰", profile)
    )

    assert structured is True
    assert profile.brand_labels == ["奔驰"]
    assert wants_unlock is False


def test_advance_series_locks_locks_mentioned_series(db_session: Session):
    """用户点名的车系必须被锁定（会话记忆）。"""
    source = make_source(db_session)
    brand = make_brand(db_session, name="测试厂", source=source)
    series = make_series(db_session, brand, name="测试A", source=source)
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="标准版", source=source)
    db_session.commit()
    profile = UserProfile()

    resolved, _structured, _unlock = asyncio.run(
        turn.advance_series_locks(db_session, "测试A怎么样", profile)
    )

    assert [s.id for s, _b in resolved] == [series.id]
    assert profile.locked_series_ids == [series.id], "点名的车系应被锁定"


def test_advance_series_locks_unlocks_on_explicit_request(db_session: Session):
    """「看看其他车」必须清空锁定集合。"""
    source = make_source(db_session)
    brand = make_brand(db_session, name="测试厂2", source=source)
    series = make_series(db_session, brand, name="测试B", source=source)
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="标准版", source=source)
    db_session.commit()
    profile = UserProfile(locked_series_ids=[series.id])

    _resolved, _structured, wants_unlock = asyncio.run(
        turn.advance_series_locks(db_session, "看看其他车", profile)
    )

    assert wants_unlock is True
    assert profile.locked_series_ids == [], "明确要求看其他车时必须解锁"


def test_advance_series_locks_does_not_lock_in_negation_context(db_session: Session):
    """否定语境（「我不买测试C」）**不加锁**（评审 P2）。"""
    source = make_source(db_session)
    brand = make_brand(db_session, name="测试厂3", source=source)
    series = make_series(db_session, brand, name="测试C", source=source)
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="标准版", source=source)
    db_session.commit()
    profile = UserProfile()

    asyncio.run(turn.advance_series_locks(db_session, "我不买测试C", profile))

    assert profile.locked_series_ids == [], "否定语境不应锁定"


# ── 两段的边界 ─────────────────────────────────────────────────────────────


def test_two_stages_have_different_boundaries(db_session: Session):
    """**段一恒执行，段二只在需要时执行**——这是不能被合并掉的边界。

    段二是否执行由调用方（engine.respond）根据闲聊与否决定；
    `advance_profile` 本身不含任何「要不要跳过」的分支。
    """
    import inspect

    src = inspect.getsource(turn.advance_profile)
    assert "is_chatty" not in src, (
        "advance_profile 不该知道「闲聊」——那是路由知识，属于调用方"
    )
    # 段一必须无条件做完三件事
    assert "soft_prefs.run_if_enabled" in src
    assert "soft_probe.apply_probe_answer" in src
    assert "profile.unknowns" in src
