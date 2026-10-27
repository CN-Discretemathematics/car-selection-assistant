"""迁移在 PostgreSQL 方言下必须能落地（2026-10-21 生产事故的回归门禁）。

## 这条门禁为什么存在

2026-10-21 生产部署失败：迁移 `d3e8f1a4b7c2` 里的

    sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("0"))

在 PostgreSQL 上直接报错：

    column "confirmed" is of type boolean but default expression is of type integer

⇒ 整个迁移事务回滚 ⇒ `alembic upgrade head` 失败 ⇒ Dockerfile 的
`alembic upgrade head && uvicorn ...` 短路 ⇒ 容器起不来 ⇒ 部署脚本回滚镜像。
生产因此停在 13 天前。

**而本地与 CI 永远测不出来**：本机与 CI 的测试库都是 SQLite，
SQLite 接受 `BOOLEAN DEFAULT 0`。「生产走 alembic upgrade head，本地开发走 create_all，
两条建表路径只要有一处不一致，缺陷就**只在生产暴露**」——这次就是那句话应验。

## 本测试做什么

1. 把**全部迁移**在 **PostgreSQL 方言**下渲染成 DDL（alembic 离线模式，不连库）；
2. 把**模型元数据**（create_all 那条路径）也在 PostgreSQL 方言下渲染；
3. 断言：布尔列的默认值**必须是布尔字面量**（true/false），不能是 0/1；
4. 断言「渲染确实覆盖了全部迁移」——否则第 3 条会空转（恒真）。

第 4 条是本仓的硬规矩：空转断言必须被变异测试抓出来，
所以另配 `.tools/mutate_pg_bool_default.py` 专门把 `false` 改回 `0`，
验证本测试会红。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = BACKEND / "alembic" / "versions"

# `name BOOLEAN DEFAULT <x>` —— 只认列定义里的 DEFAULT，
# 不去碰 CHECK 约束或别的写法（宁可漏判也不误杀）。
BOOLEAN_DEFAULT_RE = re.compile(r"(\w+)\s+BOOLEAN\s+DEFAULT\s+([^\s,)]+)", re.IGNORECASE)

# PostgreSQL 接受的布尔字面量。
BOOL_LITERALS = {"true", "false"}


def _assert_boolean_defaults_are_literals(ddl: str, where: str) -> None:
    offenders = [
        f"{where}: 列 {name} 的默认值 {value!r} 不是布尔字面量"
        for name, value in BOOLEAN_DEFAULT_RE.findall(ddl)
        if value.strip("'\"").lower() not in BOOL_LITERALS
    ]
    assert not offenders, (
        "布尔列的默认值必须是 true/false（PostgreSQL 不接受 0/1 作为 boolean 默认值）：\n"
        + "\n".join(offenders)
    )


@pytest.fixture(scope="module")
def postgres_ddl() -> str:
    """把整条迁移链在 PostgreSQL 方言下渲染成 DDL（离线，不连库）。"""
    # alembic 的 env.py 会用应用配置里的 DATABASE_URL 覆盖 URL ⇒ 这里改环境变量。
    # 离线模式只渲染 SQL，永远不会去连这个地址。
    import logging.config
    import os

    from _pytest.monkeypatch import MonkeyPatch

    from app.common.config import get_settings

    mp = MonkeyPatch()
    # ⚠️ 必须屏蔽 alembic env.py 里的 `fileConfig(...)`：
    # 它默认 `disable_existing_loggers=True`，会把**所有**已存在的 logger 置为 disabled
    # ⇒ 后续用例的 caplog 收不到任何记录（实测 20 个用例因此失败）。
    # 这就是「测试污染」的典型形态：改的不是被测代码，是全局 logging 状态。
    mp.setattr(logging.config, "fileConfig", lambda *a, **k: None)
    # 不带用户名口令：离线模式永远不连它，但 `scan_secrets.py` 会把
    # 「带凭据的数据库 URL」判成高危（即使是假的）。
    os.environ["DATABASE_URL"] = "postgresql+psycopg://127.0.0.1:5432/render_only"
    get_settings.cache_clear()
    try:
        cfg = Config(str(BACKEND / "alembic.ini"))
        # script_location 在 ini 里是相对路径（alembic）⇒ 换成绝对路径，
        # 让测试不依赖当前工作目录。
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        buf = io.StringIO()
        # ⚠️ alembic 离线模式的 SQL 走 **config.output_buffer**（environment.py:
        # `elif self.config.output_buffer is not None: opts["output_buffer"] = ...`），
        # 不是 config.stdout —— 之前只设 stdout，渲染结果整段丢失，
        # 断言「CREATE TABLE 不在结果里」差点变成一条恒真的空转断言。
        cfg.output_buffer = buf
        cfg.stdout = buf
        command.upgrade(cfg, "head", sql=True)
        return buf.getvalue()
    finally:
        os.environ.pop("DATABASE_URL", None)
        get_settings.cache_clear()
        mp.undo()


def test_migration_chain_renders_under_postgres(postgres_ddl: str) -> None:
    """迁移链必须能在 PostgreSQL 方言下完整渲染（渲染不出来 = 迁移本身写坏了）。"""
    assert "CREATE TABLE" in postgres_ddl, "离线渲染没有产出任何建表语句——这条测试会空转"
    # 本次事故涉及的两张表必须出现在渲染结果里 ⇒ 证明新迁移确实被渲染到了
    assert "preference_memories" in postgres_ddl
    assert "variant_feedbacks" in postgres_ddl
    assert "anonymous_identities" in postgres_ddl


def test_every_create_table_is_rendered(postgres_ddl: str) -> None:
    """渲染出的建表数不少于迁移源码里 op.create_table 的次数。

    防止「某条迁移没被渲染到 ⇒ 断言查不到问题 ⇒ 恒真」。
    """
    sources = "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS_DIR.glob("*.py")))
    declared = len(re.findall(r"op\.create_table\(", sources))
    rendered = len(re.findall(r"CREATE TABLE", postgres_ddl, re.IGNORECASE))
    assert declared > 0, "迁移源码里一个 op.create_table 都没有——统计口径失效"
    assert rendered >= declared, (
        f"迁移源码里有 {declared} 处 op.create_table，渲染结果只有 {rendered} 条 CREATE TABLE"
        "⇒ 有迁移没被渲染到，下面的断言会漏判"
    )


def test_migration_boolean_defaults_are_boolean_literals(postgres_ddl: str) -> None:
    """迁移路径：布尔列的默认值必须是布尔字面量（事故根因）。"""
    _assert_boolean_defaults_are_literals(postgres_ddl, "迁移路径")


def test_model_metadata_boolean_defaults_are_boolean_literals() -> None:
    """create_all 路径：模型元数据在 PostgreSQL 方言下的 DDL 同样要合法。

    两条建表路径**必须都过**这条断言——它们不一致时缺陷就只在生产暴露。
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from app.common import models  # noqa: F401  确保模型注册
    from app.common.database import Base

    dialect = postgresql.dialect()
    ddl = "\n".join(
        str(CreateTable(table).compile(dialect=dialect)) for table in Base.metadata.sorted_tables
    )
    assert "CREATE TABLE" in ddl, "模型元数据渲染不出 DDL——这条测试会空转"
    _assert_boolean_defaults_are_literals(ddl, "create_all 路径")
