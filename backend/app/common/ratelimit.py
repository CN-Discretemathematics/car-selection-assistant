"""进程内 IP 滑动窗口限流（本地开发保护；生产由 WAF/Redis 承担）。

仅作纵深防御的最小实现：按客户端 IP 计数，超限返回 429。
"""
from __future__ import annotations

import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class SlidingWindowLimiter:
    """滑动窗口计数：窗口内超过 max_requests 即拒绝。"""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.time()
        if len(self._hits) > 1024:  # IP 流变防内存无界增长（评审 L-R8）：机会式清理过期键
            self._evict(now)
        hits = [t for t in self._hits[key] if now - t < self._window]
        self._hits[key] = hits
        if len(hits) >= self._max:
            return False
        hits.append(now)
        return True

    def _evict(self, now: float) -> None:
        stale = [k for k, ts in self._hits.items() if not ts or now - ts[-1] >= self._window]
        for k in stale:
            self._hits.pop(k, None)

    def clear(self) -> None:
        self._hits.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """按客户端 IP 限流；健康检查等探针路径免限流。"""

    def __init__(
        self,
        app,
        limiter: SlidingWindowLimiter | None = None,
        max_requests: int | None = None,
        window_seconds: int = 60,
        exclude_paths: tuple[str, ...] = ("/api/v1/health",),
    ) -> None:
        super().__init__(app)
        self._limiter = limiter or SlidingWindowLimiter(
            max_requests=max_requests or 120, window_seconds=window_seconds
        )
        self._exclude_paths = exclude_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._exclude_paths:
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        if not self._limiter.allow(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试。"},
            )
        return await call_next(request)
