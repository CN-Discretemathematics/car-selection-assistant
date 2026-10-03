"""add vehicle_variants.seat_count

把「座位数」从 spec_facts 里**物化**成列，使它能和 brand/body/energy/price 一样
下推 SQL（H2）。

## 为什么物化

H2 实测（2026-10-03，本地快照库 908 车系 / 6629 款型）：
推荐链路空画像时 p50 扫 5132 个候选（占全库 78%），最坏 3.74s。诊断结论不是
「缺 LIMIT」，而是**座位数这一条硬约束结构性地无法下推**——判断「能不能坐 5 个人」
要读 `spec_facts` 里的座位数事实，而事实必须先把款型物化出来才读得到。
于是 `candidates_scanned - count` 的 p50 = 0：全表早已物化完，Python 侧再过滤
已经太晚。

## 关键设计：NULL 必须保留

物化前的判定是：

    seats = _extract_seats(facts)
    if profile.passengers is not None and seats is not None and seats < profile.passengers:
        continue

注意 `seats is not None` —— **库里没有座位数事实的款型是被保留的**（缺数据 ≠ 不满足）。
所以下推后的谓词必须是：

    (seat_count IS NULL OR seat_count >= :passengers)

写成 `seat_count >= :passengers` 会把所有**未回填**的款型悄悄丢掉，是静默改变结果。
列保持可空，正是为了让「没数据」与「座位不够」在 SQL 层仍然可区分。

## 索引

只给 `seat_count` 建单列索引：查询谓词是「在 status/brand/body/energy/price 过滤
之后再按 seat_count 排」，届时候选集已很小，复合索引的收益不抵写放大。

Revision ID: b7e4c1d90a25
Revises: c8a1f2e4b9d3
Create Date: 2026-10-03

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b7e4c1d90a25"
down_revision = "c8a1f2e4b9d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vehicle_variants",
        sa.Column("seat_count", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_variants_seat_count", "vehicle_variants", ["seat_count"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_variants_seat_count", table_name="vehicle_variants")
    op.drop_column("vehicle_variants", "seat_count")
