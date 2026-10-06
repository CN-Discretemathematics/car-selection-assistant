"""pytest 固定装置：内存 SQLite + 依赖覆盖。"""
from __future__ import annotations

import os

# 必须在导入 app 前设置：测试不触碰 dev.db，也不执行启动建表
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AUTO_CREATE_TABLES"] = "false"
# 测试需要拿到验证码（开发回显开关）；生产该值为 false 且不随代码改变
os.environ["DEV_ECHO_CODES"] = "true"
# 管理后台测试凭据
os.environ["ADMIN_API_TOKEN"] = "test-admin-token"
# 测试必须确定性：禁用 LLM（避免命中真实 DeepSeek API）、检索固定为本地 BM25
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["RETRIEVAL_BACKEND"] = "inmemory"
# 云中间件：测试统一走进程内/本地回退，不触碰真实 Redis/OSS/SMTP
os.environ["REDIS_URL"] = ""
os.environ["OSS_ENDPOINT"] = ""
os.environ["OSS_BUCKET"] = ""
os.environ["OSS_ACCESS_KEY_ID"] = ""
os.environ["OSS_ACCESS_KEY_SECRET"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["SMTP_FROM"] = ""
# 对话逐轮留档（2026-10-05）**显式置 false**，不能用 pop：
# 它的默认值是 true（留档的价值在于「出事之后查得到」，默认关等于没有），
# pop 只会退回默认 true，于是本机 .env / CI 一旦不同，测试就会开始往库里写行——
# 既拖慢测试，又让「本机跑 == CI 跑」这条前提失效。测试不需要留档。
#
# ⚠️ 必须放在 `from app.main import app` **之前**：import 链里会调用 `get_settings()`，
# 而它带 lru_cache——一旦在那之前缓存过默认值，之后再改 os.environ 已经晚了
# （本轮就踩了这个：设在文件下半段时，测试里读到的仍是 True）。
os.environ["AGENT_CONVERSATION_LOG_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.common import models  # noqa: F401  确保模型注册
from app.common.database import Base, apply_sqlite_pragmas, get_session
from app.main import app

# 路由 env 清场（必须在 app 导入之后）：本机 backend/.env 可能写了 AGENT_ROUTER_*
# （本地 shadow E2E 用），app/retrieval/config.py 的 load_dotenv() 会把它们灌进
# os.environ——测试必须对环境免疫：显式清除，恢复「未设置 → 默认」语义
# （test_env_fallbacks 依赖；2026-09-18 实测：.env 带 shadow 时 9 个模式相关测试全偏）。
for _router_env in (
    "AGENT_ROUTER_MODE",
    "AGENT_ROUTER_TIMEOUT_MS",
    "AGENT_ROUTER_MIN_CONFIDENCE",
    "AGENT_ROUTER_MODEL",
    # L1 软偏好（soft_prefs）：同一道理——本机 .env 可能写了 shadow 调试值，
    # 漏清会让「默认 off、行为不变」的测试在有 .env 的机器上偏移。
    "AGENT_SOFT_PREF_MODE",
    "AGENT_SOFT_PREF_TIMEOUT_MS",
):
    os.environ.pop(_router_env, None)


# 注意：本机受限沙箱会拒绝枚举 pytest 的 basetemp 目录，tmp_path fixture 不可用；
# 需要临时文件时请写到 tests/.tmp（已 gitignore），不要用 tmp_path。


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # 测试也必须与生产一致地开启外键：生产是 PostgreSQL（强制外键），而 SQLite
    # 默认关闭。此前夹具自建引擎、不经过 get_engine()，导致外键语义在测试中
    # 完全不生效——「只在生产暴露的数据完整性缺陷」正是这么漏掉的。
    apply_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    def override() -> Session:
        yield db_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
