"""画像反序列化的自愈行为（2026-10-02 Critical 修复）。

`respond()` 原本是裸的 `UserProfile(**await store.get_profile(...))`。任何一次
校验失败都会让 `ValidationError` 逃出 `respond`——该 session 之后**每次请求
恒 500 且不自愈**，只有「新对话」能恢复。部署时改一次 UserProfile 字段类型，
就是一次线上批量 500。

修复后：脏画像被丢弃、按空画像重新播种、记 error 级日志，用户对话继续。
"""
from __future__ import annotations

import asyncio

from app.agent.engine import _load_profile
from app.agent.schemas import UserProfile


class _FakeStore:
    """最小 SessionStore 替身：只实现 get_profile / set_profile 两个契约方法。"""

    def __init__(self, initial: dict) -> None:
        self.value = initial
        self.writes: list[dict] = []

    def get_profile(self, session_id: str) -> dict:
        return self.value

    def set_profile(self, session_id: str, data: dict) -> None:
        self.value = data
        self.writes.append(data)


def test_valid_profile_round_trips_without_rewrite():
    """正常画像原样返回，且**不**产生写回（避免每轮一次无谓的 Redis 往返）。"""
    store = _FakeStore(UserProfile().model_dump())
    profile = asyncio.run(_load_profile(store, "s1"))
    assert isinstance(profile, UserProfile)
    assert store.writes == []


def test_dirty_profile_is_healed_not_raised():
    """脏值不再抛出：重建空画像并写回 store。"""
    # budget 被写成了字符串，UserProfile 期望嵌套的 Budget 模型
    store = _FakeStore({"budget": "我也不知道多少", "unknowns": []})
    profile = asyncio.run(_load_profile(store, "s1"))
    assert isinstance(profile, UserProfile)
    assert len(store.writes) == 1, "必须写回，否则下一轮仍读到同一份脏数据"


def test_healed_profile_is_readable_on_second_call():
    """自愈必须真正收敛：第二次调用走正常路径，不再重复重建。"""
    store = _FakeStore({"budget": "我也不知道多少"})
    asyncio.run(_load_profile(store, "s1"))
    again = asyncio.run(_load_profile(store, "s1"))
    assert isinstance(again, UserProfile)
    assert len(store.writes) == 1, "自愈后不应每次请求都重建"


def test_unexpected_shape_does_not_break_the_turn():
    """完全非法的载荷（如 None / 列表）同样不应把整轮对话带崩。"""
    for bad in ([], "not-a-dict", 42):
        store = _FakeStore(bad)  # type: ignore[arg-type]
        profile = asyncio.run(_load_profile(store, "s1"))
        assert isinstance(profile, UserProfile), f"载荷 {bad!r} 应当被自愈"
