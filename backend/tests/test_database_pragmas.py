# -*- coding: utf-8 -*-
"""SQLite 侧 PRAGMA：外键必须开启（与生产 PostgreSQL 语义对齐）。

核心矛盾：生产是 PostgreSQL，**强制**外键；而 SQLite 默认**关闭**外键。
本项目测试与本地开发跑 SQLite、生产跑 PG，两边语义不一致的后果是——
所有外键/级联/孤儿数据问题只在生产暴露，本地与 CI 全绿却毫无察觉。

另两项：
- journal_mode=WAL：避免并发写时频繁 "database is locked"（内存库下无效，故不断言）
- busy_timeout=10000：默认 5s 锁等待偏短

夹具侧同步：tests/conftest.py 的 db_session 自建引擎、不经过 get_engine()，
因此也调用 apply_sqlite_pragmas()，保证「测试跑的路径」与「生产跑的路径」
在外键语义上一致。
"""
from __future__ import annotations

import pytest

import app.common.database as dbmod
from app.common.config import get_settings


@pytest.fixture
def _isolated_engine(monkeypatch):
    monkeypatch.setattr(dbmod, "_engine", None)
    monkeypatch.setattr(dbmod, "_session_factory", None)


def test_sqlite_foreign_keys_enabled(monkeypatch, _isolated_engine):
    """外键开启：与生产 PG 对齐，脏引用在本地/CI 即暴露而非留到生产。"""
    monkeypatch.setattr(get_settings(), "database_url", "sqlite://")

    engine = dbmod.get_engine()
    with engine.connect() as conn:
        enabled = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()

    assert enabled == 1, "SQLite 默认关闭外键，会让数据完整性缺陷只在生产暴露"


def test_sqlite_busy_timeout_raised(monkeypatch, _isolated_engine):
    """锁等待从默认 5s 提到 10s，降低线程池并发写失败概率。"""
    monkeypatch.setattr(get_settings(), "database_url", "sqlite://")

    engine = dbmod.get_engine()
    with engine.connect() as conn:
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()

    assert timeout == 10000
