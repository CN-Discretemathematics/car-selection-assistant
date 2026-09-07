"""应用配置。

生产环境所有敏感配置通过环境变量或云密钥管理服务注入；
本地开发可用 .env（见 .env.example）。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "car-selection-api"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./dev.db"
    # 开发便利：启动时按模型建表；生产必须设为 false，统一走 Alembic 迁移
    auto_create_tables: bool = True
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    # DeepSeek API（模型名与密钥必须配置化；密钥为空时 Agent 以确定性模式运行，不调用模型）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    # Agent 会话（本地开发为进程内存储 + TTL；生产替换为 Redis）
    agent_session_ttl_seconds: int = 3600
    # 认证（邮箱验证码 + OTP；本地进程内存储，生产 Redis）
    dev_echo_codes: bool = False  # 仅开发模式：注册/登录响应回显验证码；生产必须为 false
    auth_token_ttl_seconds: int = 30 * 86400
    auth_code_ttl_seconds: int = 300
    # 管理后台：独立凭据由运维经 KMS 注入（Bearer token），不开放注册；
    # 为空时管理后台接口返回 503（未配置）
    admin_api_token: str = ""
    # 云 Redis（会话/限流/验证码/令牌的生产存储；为空时回退进程内存储）
    redis_url: str = ""
    # OSS 对象存储（网页快照/PDF/图片；为空时仅本地存档）
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    # 验证码邮件（SMTP；为空时开发模式控制台输出，生产必须配置）
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
