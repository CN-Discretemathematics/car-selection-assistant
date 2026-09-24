# -*- coding: utf-8 -*-
"""注册/登录响应的验证码回显二次门：生产误留 DEV_ECHO_CODES 不得生效。

安全背景：dev_code 一旦在生产响应里回显，等同于任意邮箱账号可被接管。仓库已有
同类前科（config.py 注释记录：生产 .env 曾误留 auto_create_tables=true），故不能
只依赖 dev_echo_codes 单一开关，另加 APP_ENV 与 database_url 两道判定。

本组用例走真实端点（与 test_auth.py 同构），验证三种组合下的对外契约。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.common.config import get_settings


def test_dev_code_echoed_on_sqlite(client: TestClient):
    """开发/测试环境（SQLite）：开关打开即回显——现有行为不变。"""
    resp = client.post("/api/v1/auth/register", json={"email": "echo-sqlite@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dev_code"], "SQLite 环境下开关打开应照常回显（开发便利）"


def test_dev_code_hidden_on_postgres(client: TestClient, monkeypatch):
    """生产库（PostgreSQL）：即便开关仍为 true 也不回显。"""
    # 无凭据形式：仅为触发「生产库」判定，避免引入类凭据字面量触发密钥门禁
    monkeypatch.setattr(get_settings(), "database_url", "postgresql://db.internal:5432/app")
    resp = client.post("/api/v1/auth/register", json={"email": "echo-pg@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dev_code"] is None, "生产库下必须阻断回显"


def test_dev_code_hidden_by_app_env(client: TestClient, monkeypatch):
    """显式 APP_ENV=production：即便 SQLite + 开关打开也不回显。"""
    monkeypatch.setenv("APP_ENV", "production")
    resp = client.post("/api/v1/auth/register", json={"email": "echo-env@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dev_code"] is None, "APP_ENV=production 下必须阻断回显"
