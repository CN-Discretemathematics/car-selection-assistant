"""匿名身份 → 账户的**合并**（计划 Phase 3/4「账户合并与隐私」）。

## 为什么

匿名用户在**未登录**时也积累记忆与拒绝反馈（`preference_memories` /
`variant_feedbacks`）⇒ 登录后这些数据**留在匿名身份上**，账户那边看不到
⇒ 用户换了设备/清了浏览器 cookie 就「失忆」。

⇒ 登录后把匿名身份的数据**搬进账户的身份**，并让**匿名 token 失效**
（迁移的 docstring 已写明这个语义：「匿名身份登录后要失效（合并进账户）
而不是继续有效」✓）。

## 合并语义（**去重**是这里最容易出错的地方）

记忆表「一个键只留一行」（`(identity, category, key)` 唯一 ✓）⇒ 合并时
**同一个键只能留一行** ⇒ 取**较新**的那条（记忆是「当前状态」不是流水账 ✓）。
反馈表按 `(identity, variant_id)` 唯一 ✓ ⇒ 同理取较新的（理由更新 ✓）。

## 隐私

- **导出**：把该身份的记忆与反馈原样吐给用户（他有权看自己被记住了什么 ✓）。
- **删除**：把该身份的记忆与反馈**全部删掉**（撤回权 ✓）。

⚠️ 删的是**个性化数据**（记忆 + 反馈），不动账号本身与收藏
（账号注销是另一条既有语义：`DELETE /auth/me` 保留收藏 ✓）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.preferences import get_or_create_identity
from app.common.models import (
    AnonymousIdentity,
    PreferenceMemory,
    User,
    VariantFeedback,
)

_logger = logging.getLogger("app.agent.identity_merge")

#: 身份状态：active（可用）/ merged（已并入账户，token 失效 ✓）
STATUS_ACTIVE = "active"
STATUS_MERGED = "merged"


@dataclass
class MergeOutcome:
    """一次合并的结果。"""

    identity: AnonymousIdentity
    #: 账户身份的新 token（**仅新建时**有值——明文只给这一次 ✓）
    token: str | None
    memories_moved: int
    feedbacks_moved: int


def account_identity(db: Session, user: User) -> AnonymousIdentity | None:
    """账户**自己的**匿名身份（合并的落点 ✓）。

    账户身份与「已合并进来的匿名身份」都带 `merged_user_id` ⇒ 靠
    **状态**区分：账户身份是 `active` ✓，被合并掉的是 `merged` ✓。
    """
    return db.scalar(
        select(AnonymousIdentity).where(
            AnonymousIdentity.merged_user_id == user.id,
            AnonymousIdentity.status == STATUS_ACTIVE,
        )
    )


def merge_anonymous_into_user(
    db: Session, anonymous: AnonymousIdentity, user: User
) -> MergeOutcome:
    """把匿名身份的记忆与反馈搬进**账户的身份**，并让匿名 token 失效。

    幂等 ✓：重复合并同一身份不会重复搬数据（源数据已搬空 ✓）。
    """
    target = account_identity(db, user)
    token: str | None = None
    if target is None:
        target, plaintext, _created = get_or_create_identity(db)
        token = plaintext
        target.merged_user_id = user.id
        target.status = STATUS_ACTIVE

    moved_memories = 0
    rows = db.execute(
        select(PreferenceMemory).where(PreferenceMemory.identity_id == anonymous.id)
    ).scalars().all()
    for row in rows:
        existing = db.scalar(
            select(PreferenceMemory).where(
                PreferenceMemory.identity_id == target.id,
                PreferenceMemory.category == row.category,
                PreferenceMemory.key == row.key,
            )
        )
        if existing is None:
            row.identity_id = target.id
            moved_memories += 1
        elif (row.updated_at or row.created_at) > (existing.updated_at or existing.created_at):
            # **较新**的赢（记忆是「当前状态」，不是流水账 ✓）
            existing.value = row.value
            existing.source_type = row.source_type
            existing.confidence = row.confidence
            existing.confirmed = row.confirmed
            existing.evidence_span = row.evidence_span
            existing.updated_at = row.updated_at or row.created_at
            db.delete(row)
            moved_memories += 1
        else:
            db.delete(row)

    moved_feedbacks = 0
    fbs = db.execute(
        select(VariantFeedback).where(VariantFeedback.identity_id == anonymous.id)
    ).scalars().all()
    for row in fbs:
        existing = db.scalar(
            select(VariantFeedback).where(
                VariantFeedback.identity_id == target.id,
                VariantFeedback.variant_id == row.variant_id,
            )
        )
        if existing is None:
            row.identity_id = target.id
            moved_feedbacks += 1
        elif (row.updated_at or row.created_at) > (existing.updated_at or existing.created_at):
            existing.reason = row.reason
            existing.updated_at = row.updated_at or row.created_at
            db.delete(row)
            moved_feedbacks += 1
        else:
            db.delete(row)

    # 匿名身份**失效**（token 不再可用 ✓）——但保留行做审计（merged_user_id 指向账户 ✓）
    anonymous.status = STATUS_MERGED
    anonymous.merged_user_id = user.id
    db.commit()
    _logger.info(
        "匿名身份已并入账户: %s",
        json.dumps(
            {
                "anonymous_id": anonymous.id,
                "user_id": user.id,
                "memories_moved": moved_memories,
                "feedbacks_moved": moved_feedbacks,
            },
            ensure_ascii=False,
        ),
    )
    return MergeOutcome(
        identity=target,
        token=token,
        memories_moved=moved_memories,
        feedbacks_moved=moved_feedbacks,
    )


def export_identity_data(db: Session, identity: AnonymousIdentity) -> dict:
    """导出该身份的全部个性化数据（记忆 + 拒绝反馈）✓。

    用户有权知道**系统记住了他什么** ✓ ——「记忆」是黑箱的话，
    「个性化」就谈不上可信 ✓。
    """
    memories = db.execute(
        select(PreferenceMemory)
        .where(PreferenceMemory.identity_id == identity.id)
        .order_by(PreferenceMemory.id)
    ).scalars().all()
    feedbacks = db.execute(
        select(VariantFeedback)
        .where(VariantFeedback.identity_id == identity.id)
        .order_by(VariantFeedback.id)
    ).scalars().all()
    return {
        "identity_id": identity.id,
        "status": identity.status,
        "memories": [
            {
                "id": m.id,
                "category": m.category,
                "key": m.key,
                "value": m.value,
                "source_type": m.source_type,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in memories
        ],
        "variant_feedbacks": [
            {
                "id": f.id,
                "variant_id": f.variant_id,
                "series_id": f.series_id,
                "reason": f.reason,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in feedbacks
        ],
    }


def delete_identity_data(db: Session, identity: AnonymousIdentity) -> dict:
    """删掉该身份的全部个性化数据（记忆 + 拒绝反馈）✓。

    删完这个身份就是「失忆」的 ✓（下次会话不再回填任何东西 ✓）。
    """
    memories = db.execute(
        select(PreferenceMemory).where(PreferenceMemory.identity_id == identity.id)
    ).scalars().all()
    feedbacks = db.execute(
        select(VariantFeedback).where(VariantFeedback.identity_id == identity.id)
    ).scalars().all()
    for row in [*memories, *feedbacks]:
        db.delete(row)
    db.commit()
    return {"memories_deleted": len(memories), "feedbacks_deleted": len(feedbacks)}
