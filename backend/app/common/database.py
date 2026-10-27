"""数据库引擎与会话。

- 开发/测试默认 SQLite（仅限本地）；
- 生产使用云托管 PostgreSQL，通过 DATABASE_URL 注入；
- 所有列类型保持可移植，禁止 Postgres 专有类型，保证一套迁移两端可用。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.common.config import get_settings


class Base(DeclarativeBase):
    pass


def normalize_database_url(url: str) -> str:
    """把 postgresql:// 归一为 postgresql+psycopg://（SQLAlchemy 默认驱动为 psycopg2，本项目用 psycopg v3）。"""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def describe_database_url(url: str | None) -> str:
    """连接串的**脱敏描述**：只说「哪个库的哪台机」，**绝不含用户名与口令**。

    ## 为什么需要（2026-10-21 代码审查 Blocker）

    `backend/tools/smoke_durable_memory.py` 曾把 `DATABASE_URL` **原样打印**。
    生产连接串带「用户名:口令@主机」前缀 ⇒ **口令明文进 CI 日志 / 终端历史 / 录屏**
    ——reviewer §1 红线第 7 条（日志里回显密钥明文）。而那个工具的用法注释就写着
    「真 PostgreSQL（CI / 生产同构）」⇒ 它正是会被拿去连生产的那一个。

    这不是假想：生产库口令进过会话输出（该事故已复盘，口令亦已轮换）——
    **同一个坑已经踩过一次**。

    ⇒ 这里给出**唯一**的打印口径：调用方把连接串交给本函数，只输出主机与库名。
    **用户名也不输出**——它不是机密，但没有展示价值，而最小暴露面永远是对的。

    ## 口径示例

    输入（生产连接串，带「用户名:口令@」前缀）
        → 输出：`postgresql+psycopg://<主机>:5432/<库名>（口令已隐藏）`

    输入（本地 SQLite 路径）
        → 输出：`sqlite:///./dev.db`

    ## 解析不了就**什么都不显示**

    解析失败时回显原文＝把口令又漏一次 ⇒ 宁可显示「无法解析」也不回显。
    """
    raw = (url or "").strip()
    if not raw:
        return "<未设，走默认>"
    try:
        parsed = make_url(raw)
        drv = parsed.drivername
        # ⚠️ 用 `get_backend_name()` 而不是 `parsed.dialect`——后者在 SQLAlchemy 的
        # `URL` 上**不存在**（实测：AttributeError）。这里曾因此把所有输入都吞成
        # 「无法解析」⇒ 窄的 except 才是对的：**解析类错误**降级，**代码错误**炸出来。
        if parsed.get_backend_name() == "sqlite":
            # sqlite 没有主机与用户：路径就在 database 里（`sqlite://` ⇒ 内存库）。
            # ⚠️ 用三个斜杠：SQLAlchemy 自己渲染 `sqlite:///./dev.db` 也是这个形状
            # （少一个斜杠就成了「相对路径」，读的人会误以为是别的引擎的相对路径）。
            return f"{drv}:///{parsed.database}" if parsed.database else f"{drv}://"
        host = parsed.host or "<无主机>"
        # ⚠️ `URL.port` 在端口不是整数时**抛 ValueError**（不是返回 None）
        port = f":{parsed.port}" if parsed.port else ""
        database = parsed.database or "<无库名>"
        # 「有没有口令」本身是有用信息（说明这条连接串带凭据），但**只说有、不说是什么**
        hidden = "（口令已隐藏）" if parsed.password else ""
    except (ArgumentError, ValueError) as err:
        # 解析不了 ⇒ **什么都不显示**（回显原文＝把口令又漏一次）。
        # 只捕获解析类错误：其余异常（代码写错）必须炸出来，不能被降级掩盖。
        return f"<无法解析的连接串（已隐藏）：{type(err).__name__}>"
    return f"{drv}://{host}{port}/{database}{hidden}"


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def apply_sqlite_pragmas(engine: Engine) -> None:
    """SQLite 侧必开的 PRAGMA（仅对 sqlite 引擎注册）。

    - foreign_keys=ON：SQLite 默认**关闭**外键，而生产 PostgreSQL 强制执行。
      本项目测试与本地开发跑 SQLite、生产跑 PG，不开此开关意味着所有外键
      /级联/孤儿数据问题只在生产暴露——这是本开关的直接来源。
    - journal_mode=WAL：默认的 rollback journal 在 FastAPI 线程池并发写下
      频繁触发 "database is locked"（内存库下该 pragma 无效但不报错）。
    - busy_timeout=10000：默认锁等待 5s 偏短，并发写易失败。

    公开导出：测试夹具自建引擎时同样调用它，保证测试与生产的 SQLite 语义一致
    （否则「测试跑的路径」与「生产跑的路径」在外键上仍是两套）。
    """
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


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
        if url.startswith("sqlite"):
            apply_sqlite_pragmas(_engine)
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
