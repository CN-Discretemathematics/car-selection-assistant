"""云 Redis 客户端（会话/限流/验证码/令牌的生产存储）。

- REDIS_URL 未配置或连接失败时返回 None，调用方回退进程内存储（原则 7）；
- 连接失败不永久回退：每 60 秒重试一次（评审 M7——短暂抖动后自动恢复）；
- 连接参数从 Settings（.env / KMS）读取，密钥绝不写代码。
"""
from __future__ import annotations

import time

import redis

from app.common.config import get_settings

_client: redis.Redis | None = None
_tried: bool = False
_last_attempt: float = 0.0
RETRY_INTERVAL_SECONDS = 60.0


def get_redis() -> redis.Redis | None:
    """云 Redis 客户端（连接失败每 60 秒重试；健康连接直接复用）。

    评审 M-R5：健康客户端不得周期性重建——瞬时 ping 失败会把可用连接清空，
    调用方回退进程内存储造成多 worker 会话/令牌脑裂，且旧连接池未关闭泄漏。
    """
    global _client, _tried, _last_attempt
    if _client is not None:
        return _client
    now = time.time()
    if _tried and now - _last_attempt < RETRY_INTERVAL_SECONDS:
        return None
    _tried = True
    _last_attempt = now
    url = get_settings().redis_url
    if not url:
        return None
    try:
        client = redis.Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
        client.ping()
        _client = client
    except Exception:  # noqa: BLE001 - Redis 不可用时回退进程内存储
        _client = None
    return _client


def reset_redis() -> None:
    """测试辅助：重置连接缓存。"""
    global _client, _tried, _last_attempt
    _client = None
    _tried = False
    _last_attempt = 0.0
