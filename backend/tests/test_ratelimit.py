"""限流中间件测试（安全和限流检查）。"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.common.ratelimit import RateLimitMiddleware, SlidingWindowLimiter


def test_sliding_window_limiter():
    limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    assert limiter.allow("1.2.3.4") is True
    assert limiter.allow("1.2.3.4") is True
    assert limiter.allow("1.2.3.4") is False
    assert limiter.allow("5.6.7.8") is True  # 其他 IP 不受影响


def test_sliding_window_rolls_off():
    limiter = SlidingWindowLimiter(max_requests=1, window_seconds=0)  # 窗口为 0：立即过期
    assert limiter.allow("a") is True
    time.sleep(0.01)
    assert limiter.allow("a") is True


def test_middleware_limits_and_excludes():
    app = FastAPI()

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/home")
    def home():
        return {"cards": []}

    limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)

    with TestClient(app) as client:
        # 健康检查免限流
        for _ in range(5):
            assert client.get("/api/v1/health").status_code == 200
        # 业务接口限流
        assert client.get("/api/v1/home").status_code == 200
        assert client.get("/api/v1/home").status_code == 200
        resp = client.get("/api/v1/home")
        assert resp.status_code == 429
        assert resp.json()["detail"]
