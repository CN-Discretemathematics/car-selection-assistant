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
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
            # SQLite 侧不套用 PG 的连接池参数（语义不同，且测试用内存库走
            # 独立 StaticPool），连接池与 PRAGMA 由下方 _sqlite_pragmas 处理。
            pool_kwargs: dict[str, object] = {}
        else:
            connect_args = {}
            # 云托管 PostgreSQL（RDS / SLB）会回收空闲 TCP 连接。SQLAlchemy 默认
            # pool_pre_ping=False，池里的陈旧连接会在下次请求抛 OperationalError，
            # 表现为「上线后偶发 500、重启即恢复」——这是本参数的直接来源。
            # pool_recycle 取 1800s，小于常见 RDS 的 idle timeout。
            pool_kwargs = {
                "pool_pre_ping": True,
                "pool_recycle": 1800,
                "pool_size": 10,
                "max_overflow": 20,
                "pool_timeout": 30,
            }
        _engine = create_engine(url, connect_args=connect_args, future=True, **pool_kwargs)
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
