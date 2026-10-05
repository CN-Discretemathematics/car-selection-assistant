"""/api/v1/version：让「服务器 == main 最新」从站外可验。

背景（2026-10-05）：#65 带着 CI 红灯被合并，#66 叠上去才发现——而**站外没有任何
手段能看出服务器跑的是哪个提交**。部署脚本的 `deployed-main.sha` 只存在于服务器
本地；`/health` 返回的是写死的 `app_version`，无论部署了哪个提交都不变。
「接口 200」被当成了「已同步」。这个端点就是补上那条观测。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _get(monkeypatch=None):
    return TestClient(app).get("/api/v1/version")


def test_version_reports_injected_commit(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "a" * 40)
    monkeypatch.setenv("BUILD_TIME", "2026-10-05T03:00:00Z")
    body = _get().json()
    assert body["commit_sha"] == "a" * 40
    assert body["build_time"] == "2026-10-05T03:00:00Z"


def test_version_says_unknown_when_not_injected(monkeypatch):
    """未注入时必须说 **unknown**，不得编一个版本号。"""
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_TIME", raising=False)
    body = _get().json()
    assert body["commit_sha"] == "unknown"
    assert body["build_time"] == "unknown"


def test_version_rejects_empty_env_as_unknown(monkeypatch):
    """空串也当 unknown——compose 里 `GIT_SHA: ${GIT_SHA:-}` 传空是常态。"""
    monkeypatch.setenv("GIT_SHA", "")
    monkeypatch.setenv("BUILD_TIME", "")
    body = _get().json()
    assert body["commit_sha"] == "unknown"
    assert body["build_time"] == "unknown"


def test_version_is_always_200_and_cheap(monkeypatch):
    """纯进程内常量：不查库、不依赖外部服务，故恒 200（与 /health 同为存活面）。"""
    monkeypatch.delenv("GIT_SHA", raising=False)
    r = _get()
    assert r.status_code == 200
    assert set(r.json()) == {"app_version", "commit_sha", "build_time"}
