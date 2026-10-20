"""H1：存储实现不得被「首次不可达」永久锁死在进程内（2026-10-21）。

## 缺陷是什么

4 处（Agent 会话 / 登录验证码 / 登录令牌 / 限流器）各自写着同一段：
「探一次 Redis → 有就用 Redis、没有就用进程内 → **永久缓存**」。
于是 **Redis 首次不可达就把实现锁死在进程内**：

- **多 worker 脑裂**：会话只在拿到它的那个 worker 里（用户换个 worker 就
  「会话不存在」）；登录令牌同理 → **用户被随机踢下线**；验证码在另一个 worker
  上验不过 → **登录不了**；限流计数各算各的。
- 且 `get_redis()` 自带的「每 60 秒重试」被这一层的一次性缓存**彻底废掉**——
  Redis 恢复了也不自愈（修复前实测：恢复后仍返回同一个进程内实例）。

## 本组用例钉住三件事

1. **回退态同一 worker 内是同一个实例**——否则会话立刻丢（比脑裂更糟）；
2. **Redis 恢复且过了重试窗口 → 切回 Redis**（自愈）；
3. **窗口内不重探**——否则每次调用都去 ping 一遍 Redis。
"""
from __future__ import annotations

import logging

import fakeredis
import pytest

from app.agent import session as session_mod
from app.agent.session import RedisSessionStore, SessionStore
from app.auth import security as security_mod
from app.auth.security import (
    RedisCodeStore,
    RedisRateLimiter,
    RedisTokenStore,
)
from app.common import redis_client as rc_mod
from app.common.redis_client import StoreSelector


@pytest.fixture()
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=False)


@pytest.fixture(autouse=True)
def _clean_selectors():
    """每个用例前后重置 4 处选择器与 Redis 客户端缓存（模块级状态）。"""
    selectors = [
        session_mod._session_store_selector,
        security_mod._code_store_selector,
        security_mod._token_store_selector,
        security_mod._rate_limiter_selector,
    ]
    for sel in selectors:
        sel.reset()
    rc_mod.reset_redis()
    yield
    for sel in selectors:
        sel.reset()
    rc_mod.reset_redis()


class _FakeStore:
    """每次新建都是**不同对象**的假存储。

    ⚠️ 不能用常量元组（如 `("local-store", None)`）代替：CPython 会把常量元组
    折叠进代码对象，**每次调用返回同一个对象**——于是「两次调用是不是同一个实例」
    这条断言恒真，测试变空转（2026-10-21 变异测试 M2 实测踩到）。
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind


def _make_selector():
    return StoreSelector(
        redis_factory=lambda client: _FakeStore("redis"),
        local_factory=lambda: _FakeStore("local"),
        name="测试存储",
    )


def test_fallback_keeps_the_same_local_instance(monkeypatch):
    """回退态同一 worker 内必须是**同一个**实例——否则会话立刻丢。"""
    monkeypatch.setattr(rc_mod, "get_redis", lambda: None)
    sel = _make_selector()
    first = sel.get()
    second = sel.get()
    assert first is second, "回退态每次都新建实例 = 会话立刻丢，比脑裂更糟"
    assert first.kind == "local"


def test_switches_back_to_redis_after_recovery(monkeypatch, fake_redis):
    """Redis 恢复且过了重试窗口 → 切回 Redis（这才是「不永久回退」）。"""
    monkeypatch.setattr(rc_mod, "get_redis", lambda: None)
    sel = _make_selector()
    assert sel.get().kind == "local"

    monkeypatch.setattr(rc_mod, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(rc_mod, "RETRY_INTERVAL_SECONDS", 0.0)
    assert sel.get().kind == "redis", "Redis 恢复后必须自愈"


def test_reprobe_keeps_the_same_local_instance(monkeypatch):
    """回退态**重新探测**后仍用同一个实例——重建等于把进程内的会话全丢了。

    ⚠️ 这条是 2026-10-21 实测补上的：修复的第一版在「探不到 Redis」时
    重建了进程内实例，于是每次重试窗口一过，进程内累积的会话就被清空
    （全量测试 7 条红，报 KeyError(session_id)）——**自己犯了一遍正要修的脑裂**。
    """
    monkeypatch.setattr(rc_mod, "get_redis", lambda: None)
    monkeypatch.setattr(rc_mod, "RETRY_INTERVAL_SECONDS", 0.0)  # 每次都过窗口
    sel = _make_selector()
    first = sel.get()
    second = sel.get()
    assert first is second, "重新探测不得重建进程内实例（会话会当场丢光）"
    assert second.kind == "local"


def test_does_not_reprobe_before_the_window(monkeypatch, fake_redis):
    """窗口内不重探——否则每次调用都去 ping 一遍 Redis。"""
    probes = {"n": 0}

    def _probe():
        probes["n"] += 1
        return None

    monkeypatch.setattr(rc_mod, "get_redis", _probe)
    sel = _make_selector()
    sel.get()
    sel.get()
    sel.get()
    assert probes["n"] == 1, f"窗口内不该重复探 Redis，实际探了 {probes['n']} 次"


def test_switch_is_recorded_in_the_log(monkeypatch, fake_redis, caplog):
    """切换必须**显式记录**——进程内的会话/令牌在切换后失效，用户要知道自己被踢了。"""
    monkeypatch.setattr(rc_mod, "get_redis", lambda: None)
    sel = _make_selector()
    sel.get()

    monkeypatch.setattr(rc_mod, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(rc_mod, "RETRY_INTERVAL_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger="app.common.redis"):
        sel.get()
    warns = [r for r in caplog.records if r.name == "app.common.redis"]
    assert warns, "切回 Redis 必须落 warning（否则是静默行为变更）"
    assert "Redis 已恢复" in warns[-1].getMessage()


# ── 4 处调用点：各自都要能自愈 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "module,attr,redis_cls,local_cls",
    [
        (session_mod, "_session_store_selector", RedisSessionStore, SessionStore),
        (security_mod, "_code_store_selector", RedisCodeStore, None),
        (security_mod, "_token_store_selector", RedisTokenStore, None),
        (security_mod, "_rate_limiter_selector", RedisRateLimiter, None),
    ],
)
def test_each_call_site_switches_back(
    monkeypatch, fake_redis, module, attr, redis_cls, local_cls
):
    """会话 / 验证码 / 令牌 / 限流器——**每一处**都不得被永久锁死。"""
    monkeypatch.setattr(rc_mod, "get_redis", lambda: None)
    sel = getattr(module, attr)
    fallback = sel.get()
    if local_cls is not None:
        assert isinstance(fallback, local_cls), f"{attr} 应回退到 {local_cls.__name__}"

    monkeypatch.setattr(rc_mod, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(rc_mod, "RETRY_INTERVAL_SECONDS", 0.0)
    recovered = sel.get()
    assert isinstance(recovered, redis_cls), (
        f"{attr} 在 Redis 恢复后仍是 {type(recovered).__name__}，"
        f"没有切回 {redis_cls.__name__}"
    )


def test_engine_does_not_hold_a_stale_store(monkeypatch, fake_redis):
    """引擎**不能缓存** store 实例——存储实现会切换，缓存的引用会与路由分叉。

    实测踩过（2026-10-21）：引擎在构造时把实例存下来，路由现取 →
    切换后路由在旧实例里建会话、引擎往新实例里写画像
    → `KeyError(session_id)`，全量测试 7 条红。
    """
    from app.agent.engine import AgentEngine

    monkeypatch.setattr(rc_mod, "get_redis", lambda: None)
    engine = AgentEngine()
    first = engine._store
    assert first is not None

    monkeypatch.setattr(rc_mod, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(rc_mod, "RETRY_INTERVAL_SECONDS", 0.0)
    second = engine._store
    assert second is not first, "引擎必须每次现取 store（否则与路由分叉）"
    assert isinstance(second, RedisSessionStore)
