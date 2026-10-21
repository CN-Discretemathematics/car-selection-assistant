"""Agent 接口。

- POST   /agent/sessions                          创建会话
- POST   /agent/sessions/{session_id}/messages    发送消息（同步返回结构化结果）
- GET    /agent/sessions/{session_id}/stream      SSE：重放会话最近一次结果事件
- POST   /agent/sessions/{session_id}/reset       清空会话记忆（画像/消息/上次结果），会话继续可用
- DELETE /agent/sessions/{session_id}             删除会话（其后按会话不存在处理）
- POST   /agent/identity                         领取匿名身份（跨会话记忆用）
- GET    /agent/preferences                      列出该身份已记住的偏好
- DELETE /agent/preferences/{memory_id}           删除单条记忆
生产环境由 Redis Stream 做事件中转；本地开发为进程内存储（session.py）。

为什么要重置接口：Agent 的用户画像是**会话内累积**的，某一轮把约束理解错（例如把
「不要奔驰」记成正向约束）会一直带到后续每轮；此前只能靠刷新页面换新会话，排障与
用户自助都没有正式入口。

为什么匿名身份走 Bearer token 而不是 Cookie：本仓的登录态本来就是
`Authorization: Bearer <token>`（`auth/router.py::get_current_user`），
匿名身份沿用同一套可避免引入第二套凭据传递方式，也免掉 CSRF 面。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.agent.engine import get_agent_engine
from app.agent.preferences import (
    get_identity_by_token,
    get_or_create_identity,
    list_memories,
    mode_capabilities,
)
from app.agent.schemas import (
    AgentMessageIn,
    AgentMessageOut,
    IdentityOut,
    PreferenceMemoryOut,
    SessionOut,
)
from app.agent.session import get_session_store
from app.common.database import get_session
from app.common.errors import not_found
from app.common.models import AnonymousIdentity

router = APIRouter(tags=["agent"])


def _bearer(authorization: str | None) -> str:
    """从 `Authorization: Bearer x` 取 token；没有则返回空串。"""
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization[len("Bearer ") :].strip()


def _resolve_identity(db: Session, authorization: str | None) -> AnonymousIdentity | None:
    """按 Bearer token 解析匿名身份；没有或无效则返回 None。

    **不报错**——记忆相关接口对未携带身份的请求应返回空结果，
    而不是 401（否则前端每次都要先领身份才能问任何东西）。
    """
    token = _bearer(authorization)
    if not token:
        return None
    return get_identity_by_token(db, token)


@router.post("/agent/sessions", response_model=SessionOut, status_code=201)
def create_session() -> SessionOut:
    return SessionOut(session_id=get_session_store().create())


@router.post("/agent/sessions/{session_id}/messages", response_model=AgentMessageOut)
async def send_message(
    session_id: str,
    payload: AgentMessageIn,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> AgentMessageOut:
    # 会话存储可能是云 Redis：同步读放到线程池，避免慢连接阻塞事件循环（评审 M-R1）
    exists = await run_in_threadpool(get_session_store().exists, session_id)
    if not exists:
        raise not_found(f"会话不存在：{session_id}")
    # 匿名身份（跨会话记忆）：没有或无效则 None，**不报错**——
    # 前端没领身份也能正常问车，只是没有跨会话记忆。
    #
    # ⚠️ 闸门关着时**连查都不查**：`off` = 完全不介入，而每次请求多一次
    # 按 token_hash 的 SELECT 是白付的代价（token_hash 有索引，但不查是零）。
    #
    # ⚠️ `shadow` 档**必须解析身份**（2026-10-21 实测）：影子日志
    # （「偏好记忆（shadow，未写库）」/「拒绝反馈（shadow，未写库）」）
    # 都以「有身份」为前提 ⇒ 这里若按「只在 on 才解析」来判，
    # `shadow` 档的身份恒为 None ⇒ 影子日志永不触发 ⇒ 观察期是**假的**。
    #
    # ⇒ 按**能力**取，不按档位名字猜：`mode_capabilities().observe`
    # = `shadow` 与 `on` 都解析，只有 `off` 不解析（见
    # `app/agent/preferences.py::ModeCapabilities`）。
    identity = _resolve_identity(db, authorization) if mode_capabilities().observe else None
    return await get_agent_engine().handle(
        db, session_id, payload.message, identity=identity
    )


@router.post("/agent/sessions/{session_id}/reset", status_code=204)
async def reset_session(session_id: str) -> Response:
    """清空该会话的记忆（画像 / 消息 / 上次结果），会话 ID 继续可用。

    用于「重新开始这段对话」：清掉累计约束，不要求用户刷新页面换新会话。
    """
    cleared = await run_in_threadpool(get_session_store().clear, session_id)
    if not cleared:
        raise not_found(f"会话不存在：{session_id}")
    return Response(status_code=204)


@router.delete("/agent/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str) -> Response:
    """删除会话（Redis 下三个键一并删除）；其后任何请求按会话不存在处理。"""
    deleted = await run_in_threadpool(get_session_store().delete, session_id)
    if not deleted:
        raise not_found(f"会话不存在：{session_id}")
    return Response(status_code=204)


@router.get("/agent/sessions/{session_id}/stream")
async def stream(session_id: str) -> StreamingResponse:
    """SSE：重放该会话最近一次 Agent 结果事件（本地开发实现；生产为 Redis Stream 订阅）。"""
    # 会话存储可能是云 Redis：同步读放到线程池，避免单 worker 事件循环被慢连接阻塞
    exists = await run_in_threadpool(get_session_store().exists, session_id)
    if not exists:
        raise not_found(f"会话不存在：{session_id}")

    async def event_source():
        last = await run_in_threadpool(get_session_store().get_last_result, session_id)
        if last is not None:
            yield "event: message\n"
            yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
        yield "event: done\n"
        yield "data: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 匿名身份与跨会话偏好记忆 ────────────────────────────────────────────────


@router.post("/agent/identity", response_model=IdentityOut, status_code=201)
def create_identity(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> IdentityOut:
    """领取一个匿名身份（跨会话记忆的载体）。

    返回的 `token` 由**客户端保存**并随 `Authorization: Bearer <token>` 带上。
    库里只存 token 的哈希——库被拖走也无法反推出可用凭据。

    已携带有效 token 时**返回同一个身份**（`created=false`，token 为空）——
    客户端重装页面、token 还在手上时不该凭空多出一个身份，让旧记忆失联。
    """
    identity = _resolve_identity(db, authorization)
    if identity is not None:
        return IdentityOut(identity_id=identity.id, token="", created=False)
    identity, plaintext, created = get_or_create_identity(db)
    db.commit()
    return IdentityOut(
        identity_id=identity.id,
        # 明文 token **只在新建时给一次**——之后库里只有哈希，无法再取回
        token=plaintext or "",
        created=created,
    )


@router.get("/agent/preferences", response_model=list[PreferenceMemoryOut])
def list_preferences(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> list[PreferenceMemoryOut]:
    """列出该身份已记住的偏好（供用户自查 / 纠正）。

    未携带身份或身份无效时返回**空列表**而非 401——前端可以无条件调用。
    """
    identity = _resolve_identity(db, authorization)
    if identity is None:
        return []
    return [
        PreferenceMemoryOut(
            id=m.id,
            category=m.category,
            key=m.key,
            value=m.value,
            source_type=m.source_type,
            confidence=m.confidence,
            confirmed=m.confirmed,
            created_at=m.created_at,
            expires_at=m.expires_at,
        )
        for m in list_memories(db, identity)
    ]


@router.delete("/agent/preferences/{memory_id}", status_code=204)
def delete_preference(
    memory_id: int,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> Response:
    """删除一条记忆（用户撤回某条偏好）。

    记忆被删后，**下一次**会话起不再生效——已累积的会话画像不受影响
    （那是会话内状态，用户可以在会话里重置）。
    """
    identity = _resolve_identity(db, authorization)
    if identity is None:
        raise not_found("记忆不存在或无权访问。")
    from app.common.models import PreferenceMemory

    mem = db.get(PreferenceMemory, memory_id)
    if mem is None or mem.identity_id != identity.id:
        raise not_found("记忆不存在或无权访问。")
    db.delete(mem)
    db.commit()
    return Response(status_code=204)
