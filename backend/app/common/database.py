"""数据库引擎与会话。

- 开发/测试默认 SQLite（仅限本地）；
- 生产使用云托管 PostgreSQL，通过 DATABASE_URL 注入；
- 所有列类型保持可移植，禁止 Postgres 专有类型，保证一套迁移两端可用。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.common.config import get_settings


class Base(DeclarativeBase):
    pass


def normalize_database_url(url: str) -> str:
    """把 postgresql:// 归一为 postgresql+psycopg://（SQLAlchemy 默认驱动为 psycopg2，本项目用 psycopg v3）。"""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = normalize_database_url(get_settings().database_url)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def create_all() -> None:
    """开发期建表便利方法；生产禁用（走 Alembic 迁移）。"""
    from app.common import models  # noqa: F401  确保模型注册

    Base.metadata.create_all(bind=get_engine())
