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

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.common import models  # noqa: F401  确保模型注册
from app.common.database import Base, get_session
from app.main import app

# 注意：本机受限沙箱会拒绝枚举 pytest 的 basetemp 目录，tmp_path fixture 不可用；
# 需要临时文件时请写到 tests/.tmp（已 gitignore），不要用 tmp_path。


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
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
