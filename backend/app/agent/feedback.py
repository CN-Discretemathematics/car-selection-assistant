"""用户**拒绝过**的款型——跨会话记住（计划 Phase 2b「反馈表」）。

## 为什么要有它

会话内拒绝（`profile.rejected_variant_ids`）只在**本次对话**有效 ⇒ 下次会话
又会把用户明确说过「不要」的车推给他。拒绝是**最强的负反馈**：用户说「这台不要」
就是不要；忘了它等于没听见。

## 与偏好记忆的分工

- `preferences.py`：**偏好**（预算/用途/排除项…）——封闭词表、键值形态；
- 本模块：**事件**（拒绝过哪一款、为什么）——每次拒绝一行，可带原因。

## 读回路径不新造排除机制

读回的款型 id 直接并进 `profile.rejected_variant_ids` ⇒ 推荐链**完全复用**
「会话内拒绝」那套排除逻辑（工具层零改动 ✓）⇒ 两者的行为必然一致
（仓库纪律：同一件事不许写两遍）。

## 模式闸门

沿用 `DURABLE_PREFS_MODE` 三模式（与偏好记忆同一套 ✓）：
- `off`：不写、不读（行为与接入前逐位相同 ✓）；
- `shadow`：只落日志（不写库、不回填）✓；
- `on`：写库并回填 ✓。

⚠️ **会话内**的拒绝**不受模式影响**（它本来就是硬约束 ✓）——
模式只管**跨会话**那一半 ✓。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.preferences import MODE_OFF, MODE_SHADOW, get_mode
from app.common.models import AnonymousIdentity, VariantFeedback, VehicleVariant

_logger = logging.getLogger("app.agent.feedback")


def record_feedback(
    db: Session,
    identity: AnonymousIdentity | None,
    variant_ids: list[int],
    *,
    reason: str | None = None,
) -> list[int]:
    """把本轮**新拒绝**的款型写进反馈表（跨会话记住）。

    `variant_ids` 是**本轮新增**的排除项（调用方用「拒绝前后的差集」算 ✓）
    ——已排除过的款型不重复写（幂等 ✓）。

    `reason` 是用户原话里的理由（可空 ✓）——它现在只做**留痕**（将来可做
    「按理由归类」的影子评估 ✓），**不参与**任何推荐判定 ✓。

    ⚠️ **自己提交**（与 `preferences.persist_hints` 同一范式 ✓）：
    `get_session` 不提交 ⇒ 只 flush 不 commit 等于没写（下次会话读不到 ✓）。
    失败只记 warning、**绝不影响**用户这一轮的正常回答 ✓。
    """
    if identity is None or not variant_ids:
        return []
    mode = get_mode()
    if mode == MODE_OFF:
        return []
    if mode == MODE_SHADOW:
        _logger.info(
            "拒绝反馈（shadow，未写库）: %s",
            json.dumps(
                {"identity_id": identity.id, "variant_ids": list(variant_ids)},
                ensure_ascii=False,
            ),
        )
        return []

    written: list[int] = []
    try:
        rows = db.execute(
            select(VehicleVariant.id, VehicleVariant.series_id).where(
                VehicleVariant.id.in_(variant_ids)
            )
        ).all()
        series_by_variant = {int(vid): int(sid) for vid, sid in rows}
        for vid in variant_ids:
            series_id = series_by_variant.get(int(vid))
            if series_id is None:
                # 款型不在库里（库重导入后 id 会变）⇒ 不写，避免留下悬空引用
                continue
            existing = db.execute(
                select(VariantFeedback).where(
                    VariantFeedback.identity_id == identity.id,
                    VariantFeedback.variant_id == int(vid),
                )
            ).scalars().first()
            if existing is not None:
                # 同一款型重复拒绝 ⇒ 更新理由（不新增行）
                if reason:
                    existing.reason = reason[:200]
                written.append(int(vid))
                continue
            db.add(
                VariantFeedback(
                    identity_id=identity.id,
                    variant_id=int(vid),
                    series_id=series_id,
                    reason=(reason or None) and reason[:200],
                )
            )
            written.append(int(vid))
        db.commit()
    except Exception:  # noqa: BLE001 —— 反馈写失败绝不影响用户这一轮的正常回答
        db.rollback()
        _logger.warning("拒绝反馈写入失败（已忽略，不影响响应）", exc_info=True)
        return []
    return written


def load_rejected_variant_ids(
    db: Session, identity: AnonymousIdentity | None
) -> list[int]:
    """这个身份**曾经拒绝过**的款型 id（跨会话）。

    `off`/`shadow` 模式**不读**（与偏好记忆同一套闸门 ✓）——行为与接入前逐位相同 ✓。
    """
    if identity is None:
        return []
    mode = get_mode()
    if mode in (MODE_OFF, MODE_SHADOW):
        return []
    rows = db.execute(
        select(VariantFeedback.variant_id).where(
            VariantFeedback.identity_id == identity.id
        )
    ).all()
    return [int(r[0]) for r in rows]
