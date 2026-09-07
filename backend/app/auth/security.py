"""认证基础设施：验证码、令牌、限流（本地进程内 + TTL，生产替换 Redis）。

接口刻意与 Redis 语义对齐（get/set/expire/计数窗口），替换实现即可切换。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from app.common.config import get_settings


@dataclass
class _Code:
    code: str
    expires_at: float
    attempts: int = 0


class CodeStore:
    """邮箱验证码：6 位数字，TTL 过期，验证次数上限。"""

    MAX_ATTEMPTS = 5

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, _Code] = {}

    def issue(self, email: str) -> str:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self._data[email] = _Code(code=code, expires_at=time.time() + self._ttl)
        return code

    def verify(self, email: str, code: str) -> bool:
        entry = self._data.get(email)
        if entry is None or entry.expires_at < time.time():
            return False
        if entry.attempts >= self.MAX_ATTEMPTS:
            self._data.pop(email, None)
            return False
        entry.attempts += 1
        if secrets.compare_digest(entry.code, code):
            self._data.pop(email, None)
            return True
        return False


class TokenStore:
    """登录令牌：不透明随机串，TTL 过期。"""

    def __init__(self, ttl_seconds: int = 30 * 86400) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[int, float]] = {}  # token -> (user_id, expires_at)

    def issue(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self._data[token] = (user_id, time.time() + self._ttl)
        return token

    def resolve(self, token: str) -> int | None:
        entry = self._data.get(token)
        if entry is None:
            return None
        user_id, expires_at = entry
        if expires_at < time.time():
            self._data.pop(token, None)
            return None
        return user_id

    def revoke(self, token: str) -> None:
        self._data.pop(token, None)


class RateLimiter:
    """简单滑动窗口限流（如验证码每小时最多 N 次）。"""

    def __init__(self, max_per_window: int = 5, window_seconds: int = 3600) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def hit(self, key: str) -> bool:
        """返回是否允许本次请求（并记录）。"""
        now = time.time()
        if len(self._hits) > 1024:  # 键流变防内存无界增长（评审 L-R8）：机会式清理过期键
            stale = [k for k, ts in self._hits.items() if not ts or now - ts[-1] >= self._window]
            for k in stale:
                self._hits.pop(k, None)
        hits = [t for t in self._hits.get(key, []) if now - t < self._window]
        if len(hits) >= self._max:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


_code_store: CodeStore | None = None
_token_store: TokenStore | None = None
_rate_limiter: RateLimiter | None = None


def get_code_store() -> CodeStore:
    global _code_store
    if _code_store is None:
        from app.common.redis_client import get_redis

        redis_client = get_redis()
        if redis_client is not None:
            _code_store = RedisCodeStore(redis_client, ttl_seconds=get_settings().auth_code_ttl_seconds)
        else:
            _code_store = CodeStore(ttl_seconds=get_settings().auth_code_ttl_seconds)
    return _code_store


def get_token_store() -> TokenStore:
    global _token_store
    if _token_store is None:
        from app.common.redis_client import get_redis

        redis_client = get_redis()
        if redis_client is not None:
            _token_store = RedisTokenStore(redis_client, ttl_seconds=get_settings().auth_token_ttl_seconds)
        else:
            _token_store = TokenStore(ttl_seconds=get_settings().auth_token_ttl_seconds)
    return _token_store


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        from app.common.redis_client import get_redis

        redis_client = get_redis()
        if redis_client is not None:
            _rate_limiter = RedisRateLimiter(redis_client)
        else:
            _rate_limiter = RateLimiter()
    return _rate_limiter


class RedisCodeStore:
    """Redis 验证码存储（多 worker 安全；接口与进程内 CodeStore 一致）。"""

    def __init__(self, client, ttl_seconds: int = 300) -> None:
        self._client = client
        self._ttl = ttl_seconds

    def _key(self, email: str) -> str:
        return f"auth:code:{email}"

    def issue(self, email: str) -> str:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        key = self._key(email)
        self._client.set(key, code, ex=self._ttl)
        self._client.delete(f"{key}:attempts")
        return code

    def verify(self, email: str, code: str) -> bool:
        key = self._key(email)
        stored = self._client.get(key)
        if stored is None:
            return False
        attempts_key = f"{key}:attempts"
        attempts = self._client.incr(attempts_key)
        if attempts == 1:
            self._client.expire(attempts_key, self._ttl)
        if attempts > CodeStore.MAX_ATTEMPTS:
            self._client.delete(key, attempts_key)
            return False
        if secrets.compare_digest(stored.decode("utf-8", "replace"), code):
            self._client.delete(key, attempts_key)
            return True
        return False


class RedisTokenStore:
    """Redis 登录令牌存储。"""

    def __init__(self, client, ttl_seconds: int = 30 * 86400) -> None:
        self._client = client
        self._ttl = ttl_seconds

    def issue(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self._client.set(f"auth:token:{token}", str(user_id), ex=self._ttl)
        return token

    def resolve(self, token: str) -> int | None:
        raw = self._client.get(f"auth:token:{token}")
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):  # 评审 P2：脏值/类型错误按无效令牌处理
            return None
        return value if value > 0 else None

    def revoke(self, token: str) -> None:
        self._client.delete(f"auth:token:{token}")


class RedisRateLimiter:
    """Redis 滑动窗口限流（INCR + 窗口过期）。"""

    def __init__(self, client, max_per_window: int = 5, window_seconds: int = 3600) -> None:
        self._client = client
        self._max = max_per_window
        self._window = window_seconds

    def hit(self, key: str) -> bool:
        redis_key = f"ratelimit:{key}"
        count = self._client.incr(redis_key)
        if count == 1:
            self._client.expire(redis_key, self._window)
        return count <= self._max
