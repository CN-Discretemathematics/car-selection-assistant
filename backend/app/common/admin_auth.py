"""管理凭据：多标签 token 鉴权 + 管理操作审计。

为什么需要（2026-09 复盘）：
- 原先只有一个静态 `ADMIN_API_TOKEN`，**认证的是「凭据」而不是「人」**——token 给出去就无法
  区分是谁在用，泄露了也只能整体更换；
- 管理操作没有审计：出事只能翻 uvicorn 访问日志，而那里打的是 Docker 网关 IP，
  连真实来访 IP 都没有。
现在：`ADMIN_API_TOKENS="ryan:<token>,nightly:<token>"` 每把 token 带标签（可单独吊销），
管理路径的**每一次**请求（含 401）都写审计：时间 / 标签 / 真实 IP / 方法 / 路径 / 状态 / 耗时。

配置兼容：旧的单值 `ADMIN_API_TOKEN` 仍然生效（标签 `legacy`）。
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from fastapi import Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.common.config import get_settings

# 需要审计的管理路径前缀（与 nginx 侧只允许本机访问的路径保持一致）
ADMIN_PATH_PREFIXES = ("/api/v1/admin", "/ops")
AUDIT_LOG_PATH = Path(".tmp") / "admin-audit.log"
AUDIT_LOG_MAX_BYTES = 2 * 1024 * 1024  # 超过则轮转为 .1，避免无限增长
LEGACY_LABEL = "legacy"


def parse_admin_tokens(raw: str) -> list[tuple[str, str]]:
    """解析 `label:token,label2:token2`；忽略空项与格式错误的项。"""
    tokens: list[tuple[str, str]] = []
    for item in (raw or "").split(","):
        entry = item.strip()
        if not entry or ":" not in entry:
            continue
        label, _, token = entry.partition(":")
        label, token = label.strip(), token.strip()
        if label and token:
            tokens.append((label, token))
    return tokens


def admin_credentials() -> list[tuple[str, str]]:
    """当前生效的 (标签, token) 列表：多标签优先，旧单值 token 以 `legacy` 兜底。"""
    settings = get_settings()
    tokens = parse_admin_tokens(getattr(settings, "admin_api_tokens", "") or "")
    legacy = (settings.admin_api_token or "").strip()
    if legacy and all(token != legacy for _, token in tokens):
        tokens.append((LEGACY_LABEL, legacy))
    return tokens


def resolve_admin_label(authorization: str | None) -> str | None:
    """校验 Bearer token 并返回其标签；无效返回 None（常量时间比较）。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    presented = authorization[len("Bearer "):].strip()
    if not presented:
        return None
    matched: str | None = None
    for label, token in admin_credentials():
        # 不 short-circuit：所有 token 都参与比较，避免通过响应时间区分标签
        if secrets.compare_digest(presented, token) and matched is None:
            matched = label
    return matched


def require_admin(request: Request, authorization: str | None = Header(default=None)) -> str:
    """FastAPI 依赖：鉴权并返回调用方标签（供业务侧记录）。

    未配置任何凭据 → 503；缺凭据/凭据无效 → 401；成功返回标签（如 ryan / nightly / legacy）。
    """
    if not admin_credentials():
        raise HTTPException(status_code=503, detail="管理后台未配置（ADMIN_API_TOKEN(S) 为空）。")
    label = resolve_admin_label(authorization)
    if label is None:
        raise HTTPException(status_code=401, detail="管理凭据无效或缺失。")
    return label


def _client_ip(request: Request) -> str:
    """真实来访 IP：uvicorn 以 --proxy-headers 启动且只信任本机 nginx，
    因此 request.client.host 已是 X-Forwarded-For 的首跳地址。"""
    client = getattr(request, "client", None)
    return getattr(client, "host", "-") or "-"


def write_audit_line(line: str) -> None:
    """把审计行写入 stdout（进 docker logs）与 .tmp/admin-audit.log（持久化，随 compose 卷）。"""
    import logging

    logging.getLogger("app.admin.audit").info(line)
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if AUDIT_LOG_PATH.exists() and AUDIT_LOG_PATH.stat().st_size > AUDIT_LOG_MAX_BYTES:
            AUDIT_LOG_PATH.replace(AUDIT_LOG_PATH.with_suffix(".log.1"))
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # 审计落盘失败不阻断请求（stdout 仍有记录）


class AdminAuditMiddleware(BaseHTTPMiddleware):
    """管理路径的请求审计（包含未通过鉴权的尝试）。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(ADMIN_PATH_PREFIXES):
            return await call_next(request)
        started = time.monotonic()
        label = resolve_admin_label(request.headers.get("authorization")) or "-"
        try:
            response = await call_next(request)
        except Exception:
            elapsed = int((time.monotonic() - started) * 1000)
            write_audit_line(
                f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] label={label} "
                f"ip={_client_ip(request)} {request.method} {path} status=500 {elapsed}ms"
            )
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        write_audit_line(
            f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] label={label} "
            f"ip={_client_ip(request)} {request.method} {path} status={response.status_code} {elapsed}ms"
        )
        return response
