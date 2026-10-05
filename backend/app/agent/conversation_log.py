"""助手对话逐轮留档（2026-10-05 新增）。

存在的理由是一条**独立的缺陷**，不是为微调做的铺垫：**在此之前全仓没有保存过
任何对话**。`SessionStore` 是进程内 dict（TTL 1 小时 / 上限 2000），生产 Redis
实现同样带 TTL——对话在一小时后随 TTL 蒸发，后果是线上答错了无法复现。

而本仓的典型缺陷形态恰恰是「**静默给出错误答案**」：确定性链路里的字符串匹配
或解包写错，既不抛异常、日志里也没有痕迹，往往也没有用户投诉——「汉字续航
是多少」被答成比亚迪汉的完整参数卡，应用返回 200、路由日志一切正常。
唯一能事后发现它的办法，是**能取回当时那一轮的输入与决策**。

所以这里存的是**复现所需的最小集**，不是全量 transcript：
用户输入、路由决策（intent / matched_rule / signals）、解析出的车系、回复正文、
当时的画像快照。拿齐这几样就能在本地把那一轮原样重放。

三条纪律：

1. **绝不阻断请求**。写日志失败只记 warning，绝不让它把用户的请求带崩。
2. **脱敏后再落库**。用户输入与回复都过 `_mask_pii`；画像快照里可能含手机号
   之类，同样过。⚠️ `_mask_pii` 只覆盖手机号 / 邮箱 / 15 位以上数字三类，
   **不覆盖姓名、地址、车牌**——这是已知缺口，见 `_mask_profile` 的注释。
3. **默认开启但可一键关**。`AGENT_CONVERSATION_LOG_ENABLED=false` 即完全停写
   （表还在，只是没有新行）。测试夹具显式关掉，见 `backend/tests/conftest.py`。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.common.config import get_settings
from app.common.models import AgentTurnLog

logger = logging.getLogger("app.agent.conversation_log")

#: 清理节流：每 PURGE_INTERVAL_SECONDS 秒最多扫一次，避免每轮都 DELETE。
#: 与 `SessionStore.PRUNE_INTERVAL_SECONDS` 同一把尺子（同为摊销成本考虑）。
PURGE_INTERVAL_SECONDS = 3600.0
_last_purged: float = 0.0


def is_enabled() -> bool:
    """是否记录对话留档（关闭时本模块所有写路径都是 no-op）。"""
    return get_settings().agent_conversation_log_enabled


def _mask_text(text: str | None) -> str:
    from app.agent.routing import _mask_pii

    return _mask_pii(text or "")


def _mask_profile(profile: Any) -> dict | None:
    """画像快照脱敏。

    ⚠️ `_mask_pii` 只认手机号 / 邮箱 / 15 位以上数字，**不认姓名与地址**。
    画像里的 `region`、`usage` 等是受控枚举，暂无泄漏风险；但若日后往画像里加
    自由文本字段（用户自述「我住在 XX 路」之类），必须同步扩这里的规则——
    否则那类文本会**原样落库**。这一点写在这里是为了下次改画像时有人看见。
    """
    if profile is None:
        return None
    if hasattr(profile, "model_dump"):
        data = profile.model_dump()
    elif isinstance(profile, dict):
        data = dict(profile)
    else:  # 兜底：结构不明就不落库，宁可少记也不要原样存
        return None
    return {key: _mask_text(value) if isinstance(value, str) else value
            for key, value in data.items()}


def record_turn(
    db: Session,
    *,
    session_id: str,
    user_text: str,
    out: Any,
    decision: Any,
    resolved: list[tuple[Any, Any]] | None = None,
    profile: Any = None,
    turn_index: int | None = None,
) -> None:
    """落一条对话轮次留档。**任何异常都只记 warning，不向上抛。**

    `turn_index` 不传时按「该会话已有留档条数」算——比从会话存储数更可靠：
    会话存储有 TTL 会清空，留档不会，两者不会错位。

    调用点必须放在响应生成之后、且**不要** await 一个可能失败的写操作——
    见 `engine.handle()` 里的 `run_in_threadpool` 包裹。
    """
    if not is_enabled():
        return
    try:
        if turn_index is None:
            turn_index = int(
                db.query(AgentTurnLog).filter(
                    AgentTurnLog.session_id == session_id
                ).count()
            )
        row = AgentTurnLog(
            session_id=session_id,
            turn_index=turn_index,
            user_text=_mask_text(user_text),
            reply_text=_mask_text(getattr(out, "explanation", None)),
            intent=getattr(decision, "intent", None),
            matched_rule=getattr(decision, "matched_rule", None),
            signals=_safe_dict(getattr(decision, "signals", None)),
            series_ids=[s.id for s, _b in (resolved or [])],
            profile_snapshot=_mask_profile(profile),
            need_clarification=getattr(out, "need_clarification", None),
        )
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001 —— 留档失败绝不影响用户请求
        db.rollback()
        logger.warning("agent 对话留档写入失败（已忽略，不影响响应）", exc_info=True)
        return
    _maybe_purge(db)


def _safe_dict(value: Any) -> dict | None:
    """signals 里可能有 set / 自定义对象，转 dict 前先确认可 JSON 化。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key, val in value.items():
        try:
            out[str(key)] = val if isinstance(val, (str, int, float, bool, list, dict, type(None))) else str(val)
        except Exception:  # noqa: BLE001
            out[str(key)] = "?"
    return out


def _maybe_purge(db: Session) -> None:
    """按保留期清理过期留档（带节流；失败不影响写入结果）。"""
    global _last_purged
    now = time.monotonic()
    if now - _last_purged < PURGE_INTERVAL_SECONDS:
        return
    _last_purged = now
    try:
        purge_expired(db)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("agent 对话留档清理失败（已忽略）", exc_info=True)


def purge_expired(db: Session) -> int:
    """删除超过保留期的留档，返回删除行数。

    保留期 `AGENT_CONVERSATION_LOG_RETENTION_DAYS`；设成 0 表示**永不清理**——
    这是有意保留的出口：法规要求留存时把它关掉自动清理、由外部流程接管。
    """
    days = get_settings().agent_conversation_log_retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = db.execute(delete(AgentTurnLog).where(AgentTurnLog.created_at < cutoff))
    db.commit()
    return int(result.rowcount or 0)


def load_session(db: Session, session_id: str) -> list[dict[str, Any]]:
    """取回一整个会话的留档，按轮次排序——**复现入口**。

    返回值已带 `turn_index`，可直接喂给回放脚本：把每一轮的 `user_text`
    原样发回 agent，比对 `intent` / `matched_rule` / `series_ids` 是否一致。
    """
    rows = db.execute(
        select(AgentTurnLog)
        .where(AgentTurnLog.session_id == session_id)
        .order_by(AgentTurnLog.turn_index, AgentTurnLog.id)
    ).scalars().all()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: AgentTurnLog) -> dict[str, Any]:
    return {
        "turn_index": row.turn_index,
        "user_text": row.user_text,
        "reply_text": row.reply_text,
        "intent": row.intent,
        "matched_rule": row.matched_rule,
        "signals": row.signals,
        "series_ids": row.series_ids,
        "profile_snapshot": row.profile_snapshot,
        "need_clarification": row.need_clarification,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
