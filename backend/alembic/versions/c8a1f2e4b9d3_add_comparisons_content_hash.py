"""add comparisons content_hash

Revision ID: c8a1f2e4b9d3
Revises: 7e9dda19f6df
Create Date: 2026-09-03 00:00:00.000000

评审 P1：匿名 POST /comparisons 无幂等——每次打开分享链接都会写 1+N 行。
加 content_hash（排序后的 variant_ids 摘要）索引列，路由按哈希复用已有对比。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c8a1f2e4b9d3"
down_revision = "7e9dda19f6df"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("comparisons", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_comparisons_content_hash", "comparisons", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_comparisons_content_hash", table_name="comparisons")
    op.drop_column("comparisons", "content_hash")
