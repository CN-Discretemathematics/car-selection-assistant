"""Agent 接口。

- POST   /agent/sessions                          创建会话
- POST   /agent/sessions/{session_id}/messages    发送消息（同步返回结构化结果）
- GET    /agent/sessions/{session_id}/stream      SSE：重放会话最近一次结果事件
- POST   /agent/sessions/{session_id}/reset       清空会话记忆（画像/消息/上次结果），会话继续可用
- DELETE /agent/sessions/{session_id}             删除会话（其后按会话不存在处理）
生产环境由 Redis Stream 做事件中转；本地开发为进程内存储（session.py）。

为什么要重置接口：Agent 的用户画像是**会话内累积**的，某一轮把约束理解错（例如把
「不要奔驰」记成正向约束）会一直带到后续每轮；此前只能靠刷新页面换新会话，排障与
用户自助都没有正式入口。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.agent.engine import get_agent_engine
from app.agent.schemas import AgentMessageIn, AgentMessageOut, SessionOut
from app.agent.session import get_session_store
from app.common.database import get_session
from app.common.errors import not_found

router = APIRouter(tags=["agent"])


@router.post("/agent/sessions", response_model=SessionOut, status_code=201)
def create_session() -> SessionOut:
    return SessionOut(session_id=get_session_store().create())


@router.post("/agent/sessions/{session_id}/messages", response_model=AgentMessageOut)
async def send_message(
    session_id: str,
    payload: AgentMessageIn,
    db: Session = Depends(get_session),
) -> AgentMessageOut:
    # 会话存储可能是云 Redis：同步读放到线程池，避免慢连接阻塞事件循环（评审 M-R1）
    exists = await run_in_threadpool(get_session_store().exists, session_id)
    if not exists:
        raise not_found(f"会话不存在：{session_id}")
    return await get_agent_engine().handle(db, session_id, payload.message)


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
