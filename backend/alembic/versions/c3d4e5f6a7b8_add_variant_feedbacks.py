"""add variant feedbacks

Revision ID: c3d4e5f6a7b8
Revises: d3e8f1a4b7c2
Create Date: 2026-10-21 11:10:00.000000

**variant_feedbacks** —— 用户明确拒绝过的款型（计划 Phase 2b「反馈表：拒绝跨会话记住」）。

## 为什么要有这张表

会话内拒绝（`profile.rejected_variant_ids`）只在**本次对话**有效 ⇒ 下次会话
又会把用户明确说过「不要」的车推给他。拒绝是**最强的负反馈**，忘了它等于没听见。

## 与 preference_memories 的分工

- `preference_memories`：**偏好**（预算/用途/排除项…）——封闭词表、键值形态；
- 本表：**事件**（拒绝过哪一款、为什么）——每行一次拒绝。

## 幂等

`(identity_id, variant_id)` 唯一 ⇒ 同一款型重复拒绝只留一行（理由更新）。
读回时并进 `profile.rejected_variant_ids` ⇒ 排除逻辑与「会话内拒绝」完全同一套。

时间列一律用带时区的 DateTime，与 agent_turn_logs / preference_memories 一致。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "d3e8f1a4b7c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "variant_feedbacks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("identity_id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["identity_id"], ["anonymous_identities.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["vehicle_variants.id"]),
        sa.ForeignKeyConstraint(["series_id"], ["vehicle_series.id"]),
        sa.UniqueConstraint(
            "identity_id", "variant_id", name="uq_variant_feedback_identity_variant"
        ),
    )
    op.create_index("ix_variant_feedback_identity_id", "variant_feedbacks", ["identity_id"])


def downgrade() -> None:
    op.drop_index("ix_variant_feedback_identity_id", table_name="variant_feedbacks")
    op.drop_table("variant_feedbacks")
