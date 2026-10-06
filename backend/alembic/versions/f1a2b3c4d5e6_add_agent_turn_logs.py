"""add agent_turn_logs

Revision ID: f1a2b3c4d5e6
Revises: b7e4c1d90a25
Create Date: 2026-10-05 20:50:00.000000

助手对话逐轮留档。存的是**复现所需的最小集**：用户输入、路由决策、
解析出的车系、回复正文、当时的画像快照——取回这几样即可在本地原样重放那一轮。

在此之前全仓没有保存过任何对话（进程内 SessionStore TTL 1 小时、生产 Redis
同样带 TTL），线上答错无法复现；且本仓典型缺陷形态是「静默给出错误答案」，
既无异常日志也未必有用户投诉，唯一能事后发现的办法就是能取回当时那一轮。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = 'f1a2b3c4d5e6'
down_revision = 'b7e4c1d90a25'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_turn_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('turn_index', sa.Integer(), nullable=False),
        sa.Column('user_text', sa.Text(), nullable=False),
        sa.Column('reply_text', sa.Text(), nullable=False),
        sa.Column('intent', sa.String(length=32), nullable=True),
        sa.Column('matched_rule', sa.String(length=96), nullable=True),
        sa.Column('signals', sa.JSON(), nullable=True),
        sa.Column('series_ids', sa.JSON(), nullable=True),
        sa.Column('profile_snapshot', sa.JSON(), nullable=True),
        sa.Column('need_clarification', sa.Boolean(), nullable=True),
        sa.Column('elapsed_ms', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_agent_turn_logs_session_created', 'agent_turn_logs', ['session_id', 'created_at']
    )
    op.create_index('ix_agent_turn_logs_session_id', 'agent_turn_logs', ['session_id'])


def downgrade() -> None:
    op.drop_index('ix_agent_turn_logs_session_id', table_name='agent_turn_logs')
    op.drop_index('ix_agent_turn_logs_session_created', table_name='agent_turn_logs')
    op.drop_table('agent_turn_logs')
