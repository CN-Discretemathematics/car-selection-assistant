"""Redis 存储实现测试（fakeredis，无需真实 Redis）。"""
from __future__ import annotations

import fakeredis

from app.agent.session import RedisSessionStore
from app.auth.security import (
    CodeStore,
    RedisCodeStore,
    RedisRateLimiter,
    RedisTokenStore,
)


def _client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=False)


def test_redis_code_store():
    client = _client()
    store = RedisCodeStore(client, ttl_seconds=300)
    email = "redis@example.com"
    code = store.issue(email)
    assert store.verify(email, "000000") is False
    assert store.verify(email, code) is True
    assert store.verify(email, code) is False  # 一次性：验证成功即删除
    assert client.get(f"auth:code:{email}") is None


def test_redis_token_store():
    client = _client()
    store = RedisTokenStore(client, ttl_seconds=3600)
    token = store.issue(42)
    assert store.resolve(token) == 42
    assert store.resolve("bogus") is None
    store.revoke(token)
    assert store.resolve(token) is None


def test_redis_rate_limiter():
    client = _client()
    limiter = RedisRateLimiter(client, max_per_window=2, window_seconds=60)
    assert limiter.hit("k") is True
    assert limiter.hit("k") is True
    assert limiter.hit("k") is False


def test_redis_session_store():
    client = _client()
    store = RedisSessionStore(client, ttl_seconds=600)
    sid = store.create()
    assert store.exists(sid)

    store.set_profile(sid, {"budget": {"max": 150000}})
    assert store.get_profile(sid)["budget"]["max"] == 150000

    store.append_message(sid, "user", "你好")
    store.append_message(sid, "assistant", "你好")
    assert len(store.history(sid)) == 2

    store.set_last_result(sid, {"recommended": []})
    assert store.get_last_result(sid) == {"recommended": []}

    # 消息上限：超出后只保留最近 MAX 条
    from app.agent.session import MAX_MESSAGES

    for i in range(MAX_MESSAGES + 5):
        store.append_message(sid, "user", f"m{i}")
    assert len(store.history(sid)) == MAX_MESSAGES


def test_inprocess_code_store_attempts_cap():
    store = CodeStore(ttl_seconds=300)
    email = "cap@example.com"
    code = store.issue(email)
    for _ in range(CodeStore.MAX_ATTEMPTS):
        store.verify(email, "000000")
    assert store.verify(email, code) is False
