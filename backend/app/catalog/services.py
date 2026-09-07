"""车型目录查询服务。

路由层只做协议转换，全部查询逻辑收敛在这里；
后续阶段（推荐、Agent 工具、对比）复用同一批函数，保证口径一致。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.models import (
    Brand,
    MonthlySales,
    OfficialPrice,
    SpecFact,
    VehicleModelYear,
    VehicleSeries,
    VehicleVariant,
)


def latest_full_month(today: date | None = None) -> str:
    """最近一个完整自然月。"""
    today = today or date.today()
    first_of_month = today.replace(day=1)
    return (first_of_month - timedelta(days=1)).strftime("%Y-%m")


def latest_sales_month(db: Session) -> str:
    """最近一个有销量数据的月份（retail/portal 口径）。

    销量数据通常月中才发布：若一律按「最近完整自然月」取数，月初会出现
    首页/详情页销量整体消失（生产故障：2026-09-02 首页 Top20 为空）。
    无任何销量数据时回退最近完整自然月。
    """
    max_month = db.scalar(
        select(func.max(MonthlySales.month)).where(
            MonthlySales.sales_type.in_(("retail", "portal"))
        )
    )
    return max_month or latest_full_month()


def get_series(db: Session, series_id: int) -> VehicleSeries | None:
    return db.get(VehicleSeries, series_id)


def get_variant(db: Session, variant_id: int) -> VehicleVariant | None:
    return db.get(VehicleVariant, variant_id)


def series_variants(
    db: Session,
    series_id: int,
    model_year_id: int | None = None,
    energy_type: str | None = None,
    on_sale_only: bool = True,
) -> list[VehicleVariant]:
    stmt = select(VehicleVariant).where(VehicleVariant.series_id == series_id)
    if on_sale_only:
        stmt = stmt.where(VehicleVariant.status == "on_sale")
    if model_year_id is not None:
        stmt = stmt.where(VehicleVariant.model_year_id == model_year_id)
    if energy_type:
        stmt = stmt.where(VehicleVariant.energy_type == energy_type)
    return list(db.scalars(stmt))


def variant_current_price(db: Session, variant_id: int) -> OfficialPrice | None:
    stmt = (
        select(OfficialPrice)
        .where(
            OfficialPrice.variant_id == variant_id,
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
        .order_by(OfficialPrice.effective_from.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def series_price_range(
    db: Session, series_id: int, on_sale_only: bool = True
) -> tuple[Decimal | None, Decimal | None]:
    """车系官方指导价区间 = 在售有效 SKU 当前指导价的 min~max。"""
    stmt = (
        select(
            func.min(OfficialPrice.price_cny),
            func.max(OfficialPrice.price_cny),
        )
        .join(VehicleVariant, OfficialPrice.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.series_id == series_id,
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
    )
    if on_sale_only:
        stmt = stmt.where(VehicleVariant.status == "on_sale")
    # 评审 P2：SQL 聚合取代 Python min/max（避免拉取全部价格行）
    low, high = db.execute(stmt).one()
    return low, high


def latest_sales(db: Session, series_id: int, month: str | None = None) -> MonthlySales | None:
    month = month or latest_sales_month(db)
    # 优先统一零售口径；无零售数据时回退门户榜单口径（如汽车之家），展示时如实标注
    for sales_type in ("retail", "portal"):
        stmt = (
            select(MonthlySales)
            .where(
                MonthlySales.series_id == series_id,
                MonthlySales.month == month,
                MonthlySales.sales_type == sales_type,
            )
            .order_by(MonthlySales.id.desc())
            .limit(1)
        )
        row = db.scalars(stmt).first()
        if row is not None:
            return row
    return None


def series_model_years(db: Session, series_id: int) -> list[VehicleModelYear]:
    stmt = (
        select(VehicleModelYear)
        .where(VehicleModelYear.series_id == series_id)
        .order_by(VehicleModelYear.year_name.desc())
    )
    return list(db.scalars(stmt))


def brand_of_series(db: Session, series: VehicleSeries) -> Brand | None:
    return db.get(Brand, series.brand_id)


def variant_facts(db: Session, variant_id: int) -> list[SpecFact]:
    stmt = (
        select(SpecFact)
        .where(SpecFact.variant_id == variant_id)
        .order_by(SpecFact.category, SpecFact.fact_key)
    )
    return list(db.scalars(stmt))
