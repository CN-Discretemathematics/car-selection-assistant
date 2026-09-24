# -*- coding: utf-8 -*-
"""数据层引擎参数：PG 连接池必须显式配置，SQLite 侧不受影响。

背景：云托管 PostgreSQL（RDS / SLB）会回收空闲 TCP 连接，而 SQLAlchemy 默认
pool_pre_ping=False，池内陈旧连接会在下次请求抛 OperationalError，表现为
「上线后偶发 500、重启即恢复」。本组用例钉住参数确实被传给 create_engine，
以及 SQLite 侧不被套用（两套语义不同，测试用内存库走独立 StaticPool）。

注：本地测试跑 SQLite，无法直连 PG 验证真实行为，故此处验证参数传递这一层；
生产侧效果需在部署后观察一轮。
"""
from __future__ import annotations

import pytest

import app.common.database as dbmod
from app.common.config import get_settings


@pytest.fixture
def _isolated_engine(monkeypatch):
    """隔离全局引擎单例，避免污染其它用例的 Session。"""
    monkeypatch.setattr(dbmod, "_engine", None)
    monkeypatch.setattr(dbmod, "_session_factory", None)


def _capture_kwargs(monkeypatch) -> dict:
    """包装 create_engine，捕获实际传入的关键字参数（仍走真实实现）。"""
    captured: dict = {}
    real = dbmod.create_engine

    def fake(url, **kwargs):
        captured.update(kwargs)
        return real(url, **kwargs)

    monkeypatch.setattr(dbmod, "create_engine", fake)
    return captured


def test_postgres_gets_pool_settings(monkeypatch, _isolated_engine):
    pytest.importorskip("psycopg", reason="需要 psycopg 才能构造 PG 引擎")
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.setattr(get_settings(), "database_url", "postgresql://u:p@db.local:5432/app")

    engine = dbmod.get_engine()

    assert engine.dialect.name == "postgresql", "URL 应归一化为 psycopg v3 驱动"
    assert captured.get("pool_pre_ping") is True, "无 pre_ping 会在连接被回收后首次请求 500"
    assert captured.get("pool_recycle") == 1800
    assert captured.get("pool_size") == 10
    assert captured.get("max_overflow") == 20
    assert captured.get("pool_timeout") == 30


def test_sqlite_has_no_pool_settings(monkeypatch, _isolated_engine):
    """SQLite 侧不套用 PG 连接池参数（语义不同，内存库走 StaticPool）。"""
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.setattr(get_settings(), "database_url", "sqlite:///./dev.db")

    engine = dbmod.get_engine()

    assert engine.dialect.name == "sqlite"
    assert "pool_pre_ping" not in captured
    assert "pool_recycle" not in captured
    assert "pool_size" not in captured
