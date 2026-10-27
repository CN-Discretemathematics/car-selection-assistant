"""add anonymous identities and preference memories

Revision ID: d3e8f1a4b7c2
Revises: f1a2b3c4d5e6
Create Date: 2026-10-20 23:10:00.000000

跨会话的持续个性化所需的两张表。

**anonymous_identities** —— 匿名身份的独立映射表。
不在 `users` 上加 `anonymous_id` 列：一个账户可对应多个匿名设备（一行放不下）；
匿名身份登录后要**失效**（合并进账户）而不是继续有效；合并还需要偏好去重语义。
仓库已有同构先例 `ExternalSeriesRef`（external_id → series_id + 唯一约束 + 独立映射表）。
token 只存哈希，明文只在新建时交给客户端一次——库被拖走也无法反推出可用凭据。

**preference_memories** —— 跨会话偏好记忆。
`category`/`key`/`value` 取值封闭（既有枚举）；`(identity, category, key, value)`
唯一约束保证**幂等**——同一句话重复说不产生重复行。
`source_type` 区分 explicit / inferred / behavioral；
`body_type`/`energy_type`/`budget` **不得**由推断产生（猜错会静默砍掉候选集）。
只有 `confirmed=True` 才写库。

时间列一律用带时区的 DateTime，与 agent_turn_logs 一致。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3e8f1a4b7c2"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anonymous_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("merged_user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["merged_user_id"], ["users.id"]),
    )
    op.create_index(
        "ix_anonymous_identities_token_hash", "anonymous_identities", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_anonymous_identities_merged_user_id", "anonymous_identities", ["merged_user_id"]
    )

    op.create_table(
        "preference_memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("identity_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False, server_default="explicit"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        # ⚠️ 布尔列的默认值必须是**布尔字面量**，不能是 0/1：
        # PostgreSQL 报 `column "confirmed" is of type boolean but default expression
        # is of type integer`，整个迁移事务回滚 ⇒ 容器起不来 ⇒ 部署回滚。
        # SQLite 接受 `DEFAULT 0`，所以本地（以及 CI）**永远测不出来**——
        # 2026-10-21 生产部署就是这么被它挡下来的。
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("evidence_span", sa.String(length=500), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["identity_id"], ["anonymous_identities.id"]),
        sa.ForeignKeyConstraint(["source_message_id"], ["agent_turn_logs.id"]),
        sa.UniqueConstraint(
            "identity_id", "category", "key", "value", name="uq_pref_memory_identity_cat_key_value"
        ),
    )
    op.create_index("ix_preference_memories_identity_id", "preference_memories", ["identity_id"])
    op.create_index(
        "ix_pref_memory_identity_category", "preference_memories", ["identity_id", "category"]
    )


def downgrade() -> None:
    op.drop_index("ix_pref_memory_identity_category", table_name="preference_memories")
    op.drop_index("ix_preference_memories_identity_id", table_name="preference_memories")
    op.drop_table("preference_memories")
    op.drop_index("ix_anonymous_identities_merged_user_id", table_name="anonymous_identities")
    op.drop_index("ix_anonymous_identities_token_hash", table_name="anonymous_identities")
    op.drop_table("anonymous_identities")
