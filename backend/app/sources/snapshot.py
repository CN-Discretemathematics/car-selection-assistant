"""数据快照导出（发布新的数据快照）。

导出与 tools/import_data.py 兼容的 JSON 载荷；快照 = 可追溯、可回滚的数据版本。
"""
from __future__ import annotations

from sqlalchemy import select
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


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def export_payload(db: Session) -> dict:
    """导出与 importer 兼容的当前状态快照（全部内容来自数据库，不产生新事实）。"""
    brands = []
    for brand in db.scalars(select(Brand).order_by(Brand.id)):
        brands.append(
            {
                "name": brand.name,
                "brand_type": brand.brand_type,
                "official_site": brand.official_site,
                "parent_company": brand.parent_company,
                "aliases": brand.aliases or [],
            }
        )

    series_list = []
    for series in db.scalars(select(VehicleSeries).order_by(VehicleSeries.id)):
        brand = db.get(Brand, series.brand_id)
        entry = {
            "brand": brand.name if brand else None,
            "name": series.name,
            "body_type": series.body_type,
            "energy_types": series.energy_types or [],
            "positioning": series.positioning,
            "official_page_url": series.official_page_url,
            "model_years": [],
        }
        for year in db.scalars(
            select(VehicleModelYear).where(VehicleModelYear.series_id == series.id).order_by(VehicleModelYear.id)
        ):
            year_entry = {"year_name": year.year_name, "launch_status": year.launch_status, "variants": []}
            for variant in db.scalars(
                select(VehicleVariant).where(VehicleVariant.model_year_id == year.id).order_by(VehicleVariant.id)
            ):
                price = db.scalars(
                    select(OfficialPrice)
                    .where(
                        OfficialPrice.variant_id == variant.id,
                        OfficialPrice.effective_to.is_(None),
                        OfficialPrice.price_type == "official_msrp",
                    )
                    .order_by(OfficialPrice.effective_from.desc())
                    .limit(1)
                ).first()
                facts = [
                    {
                        "category": f.category,
                        "fact_key": f.fact_key,
                        "value": f.fact_value,
                        "unit": f.unit,
                        "cycle": f.cycle,
                    }
                    for f in db.scalars(
                        select(SpecFact).where(SpecFact.variant_id == variant.id).order_by(SpecFact.id)
                    )
                ]
                year_entry["variants"].append(
                    {
                        "config_version": variant.config_version,
                        "powertrain": variant.powertrain,
                        "drivetrain": variant.drivetrain,
                        "energy_type": variant.energy_type,
                        "display_name": variant.display_name,
                        "package": variant.package,
                        "status": variant.status,
                        "effective_from": _iso(variant.effective_from),
                        "price_cny": float(price.price_cny) if price else None,
                        "facts": facts,
                    }
                )
            entry["model_years"].append(year_entry)
        series_list.append(entry)

    sales = [
        {
            "series": series.name,
            "month": sales.month,
            "sales_type": sales.sales_type,
            "count": sales.sales_count,
        }
        for sales, series in db.execute(
            select(MonthlySales, VehicleSeries)
            .join(VehicleSeries, MonthlySales.series_id == VehicleSeries.id)
            .order_by(MonthlySales.id)
        ).all()
    ]

    return {
        # 快照来源使用稳定名称：重复回滚导入复用同一 Source 行，不随时间戳累积；
        # source_type=other/credibility=medium：回滚导入不覆盖正式来源（importer 冲突规则）
        "source": {
            "name": "快照导出（数据快照）",
            "source_type": "other",
            "credibility": "medium",
            "verified_status": "unverified",
        },
        "brands": brands,
        "series": series_list,
        "sales": sales,
    }
