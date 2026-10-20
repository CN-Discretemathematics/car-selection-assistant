"""云 Redis 客户端（会话/限流/验证码/令牌的生产存储）。

- REDIS_URL 未配置或连接失败时返回 None，调用方回退进程内存储（原则 7）；
- 连接失败不永久回退：每 60 秒重试一次（评审 M7——短暂抖动后自动恢复）；
- 连接参数从 Settings（.env）读取，密钥绝不写代码。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

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


class StoreSelector:
    """存储实现选择器：Redis 优先；Redis 不可用时回退进程内，并**定期重试**。

    ## 为什么需要它（2026-10-21，修 P4 缺陷台账 H1）

    此前 4 处（会话 / 验证码 / 登录令牌 / 限流器）各自写着同一段：
    「探一次 Redis → 有就用 Redis、没有就用进程内 → **永久缓存**」。
    于是 **Redis 首次不可达就把实现锁死在进程内**：

    - **多 worker 脑裂**：会话在 worker A、登录令牌在 worker B → 用户被随机踢下线、
      验证码在另一个 worker 上验不过（登录不了）、限流计数各算各的；
    - 且 `get_redis()` 自带的「每 60 秒重试」被这一层的一次性缓存**彻底废掉**——
      Redis 恢复了也不会自愈（实测：恢复后仍返回同一个进程内实例）。

    本类把「选择 + 定期重试 + 切换时显式记录」收敛到**一处**。

    ## 切换的代价（知情选择）

    Redis 恢复后切回 Redis 时，**进程内累积的会话 / 令牌会失效**
    （它们只存在于某个 worker 的内存里）。这是一次性代价，换来的是
    「所有 worker 重新一致」；不切则是「永远不一致、且不自愈」。
    切换时打 warning 明说，不静默。
    """

    def __init__(
        self,
        redis_factory: Callable[[Any], Any],
        local_factory: Callable[[], Any],
        *,
        retry_seconds: float | None = None,
        name: str = "store",
    ) -> None:
        self._redis_factory = redis_factory
        self._local_factory = local_factory
        self._retry_seconds = retry_seconds
        self._name = name
        self._store: Any = None
        #: >0 表示当前是**回退态**（进程内实现），值为进入回退态的时刻
        self._fallback_at: float = 0.0
        self._lock = threading.Lock()

    def _window(self) -> float:
        """重试窗口。`None` → 取模块级 `RETRY_INTERVAL_SECONDS`（**调用时**读，
        测试可以改它来压缩窗口）。"""
        if self._retry_seconds is not None:
            return self._retry_seconds
        return RETRY_INTERVAL_SECONDS

    def get(self) -> Any:
        """取当前存储实现；回退态下过了窗口就重新探一次 Redis。"""
        with self._lock:
            now = time.time()
            store = self._store
            if store is not None:
                in_fallback = self._fallback_at > 0.0
                if not in_fallback or now - self._fallback_at < self._window():
                    return store
                # 回退态且已过重试窗口 → 重新探一次
                # （`get_redis()` 内部也有 60s 节流，两层节流不冲突）
                #
                # ⚠️ 探不到时**必须继续用原来那个进程内实例**：重建等于把进程内
                # 累积的会话/令牌全丢了——那正是本缺陷要避免的脑裂，
                # 自己犯一遍等于白修（2026-10-21 实测：全量测试 7 条红，
                # 报 KeyError(session_id)）。
            client = get_redis()
            if client is not None:
                was_fallback = self._fallback_at > 0.0
                self._store = self._redis_factory(client)
                self._fallback_at = 0.0
                if was_fallback:
                    logging.getLogger("app.common.redis").warning(
                        "Redis 已恢复：%s 的存储实现切回 Redis"
                        "（进程内累积的会话/令牌已失效，需重新登录或另开会话）",
                        self._name,
                    )
            else:
                if store is None:
                    store = self._local_factory()
                self._store = store
                self._fallback_at = time.time()
            return self._store

    def reset(self) -> None:
        """清空缓存与回退标记（测试辅助）。"""
        with self._lock:
            self._store = None
            self._fallback_at = 0.0
