"""测试用种子数据构造器（只用于测试，不属于正式数据来源）。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.common.models import (
    Brand,
    MonthlySales,
    OfficialPrice,
    Source,
    SpecFact,
    VehicleModelYear,
    VehicleSeries,
    VehicleVariant,
)


def make_source(db: Session, name: str = "官方测试来源", source_type: str = "official_site") -> Source:
    source = Source(name=name, source_type=source_type, verified_status="verified", credibility="high")
    db.add(source)
    db.flush()
    return source


def make_brand(
    db: Session,
    name: str = "示例品牌",
    brand_type: str = "domestic_nev",
    source: Source | None = None,
) -> Brand:
    brand = Brand(name=name, brand_type=brand_type, official_site=f"https://{name}.example.com", source_id=source.id if source else None)
    db.add(brand)
    db.flush()
    return brand


def make_series(
    db: Session,
    brand: Brand,
    name: str = "示例车系",
    body_type: str = "suv",
    energy_types: tuple[str, ...] = ("BEV",),
    source: Source | None = None,
) -> VehicleSeries:
    series = VehicleSeries(
        brand_id=brand.id,
        name=name,
        body_type=body_type,
        energy_types=list(energy_types),
        official_page_url="https://example.com/series",
        active_status="active",
        source_id=source.id if source else None,
    )
    db.add(series)
    db.flush()
    return series


def make_year(db: Session, series: VehicleSeries, year_name: str = "2025款") -> VehicleModelYear:
    year = VehicleModelYear(series_id=series.id, year_name=year_name, launch_status="on_sale")
    db.add(year)
    db.flush()
    return year


def make_variant(
    db: Session,
    series: VehicleSeries,
    year: VehicleModelYear,
    config_version: str = "标准版",
    powertrain: str = "纯电",
    drivetrain: str = "两驱",
    energy_type: str = "BEV",
    price_cny: Decimal | str | int | None = None,
    facts: list[tuple[str, str, str, str | None, str | None]] | None = None,
    source: Source | None = None,
) -> VehicleVariant:
    variant = VehicleVariant(
        series_id=series.id,
        model_year_id=year.id,
        display_name=f"{series.name} {year.year_name} {config_version}",
        config_version=config_version,
        powertrain=powertrain,
        drivetrain=drivetrain,
        energy_type=energy_type,
        body_type=series.body_type,
        status="on_sale",
        effective_from=date(2025, 1, 1),
        source_id=source.id if source else None,
    )
    db.add(variant)
    db.flush()
    if price_cny is not None:
        db.add(
            OfficialPrice(
                variant_id=variant.id,
                price_cny=Decimal(str(price_cny)),
                price_type="official_msrp",
                effective_from=date(2025, 1, 1),
                source_id=source.id if source else None,
            )
        )
    for category, key, value, unit, cycle in facts or []:
        db.add(
            SpecFact(
                variant_id=variant.id,
                category=category,
                fact_key=key,
                fact_value=value,
                unit=unit,
                cycle=cycle,
                source_id=source.id if source else None,
            )
        )
    db.flush()
    return variant


def make_sales(
    db: Session,
    series: VehicleSeries,
    month: str,
    count: int,
    source: Source | None = None,
    sales_type: str = "retail",
) -> MonthlySales:
    sales = MonthlySales(
        series_id=series.id,
        month=month,
        sales_type=sales_type,
        sales_count=count,
        source_id=source.id if source else None,
    )
    db.add(sales)
    db.flush()
    return sales
