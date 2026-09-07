"""清理开发样例数据（示例品牌及其全部后代），保留真实来源数据。

用法：python tools/replace_sample_data.py [--dry-run]
删除目标：品牌名以「示例」开头的品牌，及其车系/年款/SKU/价格/配置/销量/来源文档；
归并完成后空的「待分类（汽车之家销量榜）」品牌一并清理。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, select  # noqa: E402

from app.common.database import create_all, get_session_factory  # noqa: E402
from app.common.models import (  # noqa: E402
    Brand,
    Comparison,
    ComparisonItem,
    Favorite,
    MonthlySales,
    OfficialPrice,
    SourceDocument,
    SpecFact,
    VehicleModelYear,
    VehicleSeries,
    VehicleVariant,
)

PLACEHOLDER_BRAND = "待分类（汽车之家销量榜）"


def main(argv: list[str] | None = None) -> int:
    dry_run = "--dry-run" in (argv or sys.argv)
    create_all()
    factory = get_session_factory()
    with factory() as db:
        sample_brands = db.scalars(select(Brand).where(Brand.name.like("示例%"))).all()
        sample_brand_ids = [b.id for b in sample_brands]

        series = (
            db.scalars(select(VehicleSeries).where(VehicleSeries.brand_id.in_(sample_brand_ids))).all()
            if sample_brand_ids
            else []
        )
        series_ids = [s.id for s in series]

        counts: dict[str, int] = {}

        def count_stmt(name: str, stmt) -> None:
            counts[name] = counts.get(name, 0) + db.execute(stmt).rowcount

        if series_ids:
            # 1) 车系级挂载数据
            count_stmt("monthly_sales", delete(MonthlySales).where(MonthlySales.series_id.in_(series_ids)))
            count_stmt("source_documents", delete(SourceDocument).where(SourceDocument.series_id.in_(series_ids)))

        years = (
            db.scalars(select(VehicleModelYear).where(VehicleModelYear.series_id.in_(series_ids))).all()
            if series_ids
            else []
        )
        year_ids = [y.id for y in years]

        variants = (
            db.scalars(select(VehicleVariant).where(VehicleVariant.model_year_id.in_(year_ids))).all()
            if year_ids
            else []
        )
        variant_ids = [v.id for v in variants]

        if variant_ids:
            # 2) SKU 级挂载数据（价格/配置/文档/对比/收藏）
            count_stmt("official_prices", delete(OfficialPrice).where(OfficialPrice.variant_id.in_(variant_ids)))
            count_stmt("spec_facts", delete(SpecFact).where(SpecFact.variant_id.in_(variant_ids)))
            count_stmt("source_documents", delete(SourceDocument).where(SourceDocument.variant_id.in_(variant_ids)))
            count_stmt(
                "favorites",
                delete(Favorite).where(
                    Favorite.kind == "variant", Favorite.vehicle_id.in_(variant_ids)
                ),
            )
            # 对比项及其空的对比记录
            comparison_ids = [
                cid
                for (cid,) in db.execute(
                    select(ComparisonItem.comparison_id).where(ComparisonItem.variant_id.in_(variant_ids))
                ).all()
            ]
            count_stmt("comparison_items", delete(ComparisonItem).where(ComparisonItem.variant_id.in_(variant_ids)))
            if comparison_ids:
                count_stmt("comparisons", delete(Comparison).where(Comparison.id.in_(set(comparison_ids))))

        if year_ids:
            # 3) SKU → 年款 → 车系 → 品牌（自底向上）
            count_stmt("variants", delete(VehicleVariant).where(VehicleVariant.model_year_id.in_(year_ids)))
            count_stmt("model_years", delete(VehicleModelYear).where(VehicleModelYear.id.in_(year_ids)))
        if series_ids:
            count_stmt("favorites", delete(Favorite).where(Favorite.kind == "series", Favorite.vehicle_id.in_(series_ids)))
            count_stmt("series", delete(VehicleSeries).where(VehicleSeries.id.in_(series_ids)))
        if sample_brand_ids:
            count_stmt("brands", delete(Brand).where(Brand.id.in_(sample_brand_ids)))

        # 归并完成后为空的「待分类」品牌一并清理
        empty_placeholder = db.scalar(
            select(Brand)
            .where(Brand.name == PLACEHOLDER_BRAND)
            .where(~select(VehicleSeries.id).where(VehicleSeries.brand_id == Brand.id).exists())
        )
        if empty_placeholder is not None:
            counts["placeholder_brand"] = 1
            db.delete(empty_placeholder)

        if dry_run:
            db.rollback()
            print("dry-run（未写库）将删除：", counts)
        else:
            db.commit()
            print("已清理样例数据：", counts)
            print("剩余品牌：", [b.name for b in db.scalars(select(Brand)).all()])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
