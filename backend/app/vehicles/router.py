"""车型列表 / 详情 / SKU 列表接口。

- GET /vehicles                 全部车型浏览（筛选/排序/分页）
- GET /vehicles/{series_id}
- GET /vehicles/{series_id}/variants
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.catalog import services as catalog
from app.catalog.series_index import _brand_leads_series, keyword_needle, keyword_score
from app.common.database import get_session
from app.common.enums import MISSING_VALUE_LABEL
from app.common.errors import not_found
from app.common.freshness import data_freshness
from app.common.images import proxy_image_url
from app.common.models import VehicleVariant
from app.vehicles import listing
from app.vehicles.schemas import (
    BrandRef,
    ExternalLinkOut,
    LatestSales,
    ModelYearOut,
    PriceRange,
    SpecFactOut,
    VariantOut,
    VariantPriceOut,
    VehicleDetailOut,
    VehicleListItemOut,
    VehicleListOut,
)
from app.variants.normalization import display_fact_value, fact_display_label

router = APIRouter(tags=["vehicles"])


@router.get("/vehicles", response_model=VehicleListOut)
def vehicle_list(
    q: str | None = Query(
        default=None,
        max_length=40,
        description="关键词：车系名 / 品牌名 / 别名（忽略大小写与分隔符）",
    ),
    brand_type: str | None = Query(default=None, description="品牌类别：domestic_nev/luxury/japanese/american/german/other_fuel"),
    body_type: Literal["sedan", "suv", "mpv", "pickup"] | None = Query(default=None),
    energy_type: str | None = Query(default=None, description="BEV/PHEV/EREV/HEV/ICE/new_energy/fuel"),
    price_min: float | None = Query(default=None, description="最低价（元）"),
    price_max: float | None = Query(default=None, description="最高价（元）"),
    sort: Literal["sales_desc", "price_asc", "price_desc"] = Query(default="sales_desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_session),
) -> VehicleListOut:
    """车型列表：过滤 / 聚合 / 排序 / 分页**下推 SQL**（R7.2 无界查询禁令）。

    关键词的**相关度排序仍由 `keyword_score` 独占**（Python 侧）——
    它是「搜得到与问得到一致」的口径唯一主人，不在 SQL 里重写一遍
    （重写就是漂移）。有关键词时 SQL 只负责把过滤后的候选集取出来。
    """
    needle = keyword_needle(q)
    rows, total, page = listing.list_series(
        db,
        brand_type=brand_type,
        body_type=body_type,
        energy_type=energy_type,
        price_min=price_min,
        price_max=price_max,
        sort=sort,
        page=page,
        page_size=page_size,
        keyword=needle or None,
    )

    if needle:
        # 相关度优先 → 口径由 keyword_score 决定（0 精确 / 1 前缀或品牌 / 2 子串）
        scores: dict[int, int] = {}
        matched: list[listing.SeriesRow] = []
        for row in rows:
            score = keyword_score(row.series, row.brand, needle)
            if score is None:
                continue
            scores[row.series.id] = score
            matched.append(row)
        rows = _sort_by_relevance(matched, scores, sort)
        total = len(rows)
        total_pages = max((total + page_size - 1) // page_size, 1)
        # 越界页码钳制到最后一页（评审 P2：page=999 直接空列表不友好）
        if page > total_pages:
            page = total_pages
        start = (page - 1) * page_size
        rows = rows[start : start + page_size]

    out_items: list[VehicleListItemOut] = []
    for row in rows:
        series, brand = row.series, row.brand
        out_items.append(
            VehicleListItemOut(
                series_id=series.id,
                series_name=series.name,
                brand_id=brand.id,
                brand_name=brand.name,
                show_brand_prefix=not _brand_leads_series(brand.name or "", series.name),
                brand_type=brand.brand_type,
                body_type=series.body_type,
                energy_types=series.energy_types or [],
                thumbnail_url=proxy_image_url(series.thumbnail_url),
                price_range=PriceRange(min=row.pmin, max=row.pmax),
                price_range_note=series.price_range_note,
                latest_sales=LatestSales(
                    month=row.sale_month,
                    sales_count=row.sale_count,
                    sales_type=row.sale_type,
                    source_name=row.sale_source_name,
                ),
                source_name=row.source_name,
                data_updated_at=data_freshness(
                    series_verified_at=series.last_verified_at,
                    sales_verified_at=row.sale_verified_at,
                ),
            )
        )
    return VehicleListOut(total=total, page=page, page_size=page_size, items=out_items)


def _sort_by_relevance(
    rows: list[listing.SeriesRow], scores: dict[int, int], sort: str
) -> list[listing.SeriesRow]:
    """关键词路径的排序：相关度优先，其后与无关键词路径同口径。"""
    if sort == "price_asc":
        def key(r: listing.SeriesRow):
            return (scores.get(r.series.id, 0), r.pmin is None, r.pmin or 0)
    elif sort == "price_desc":
        def key(r: listing.SeriesRow):
            return (scores.get(r.series.id, 0), r.pmax is None, -(r.pmax or 0))
    else:
        def key(r: listing.SeriesRow):
            return (
                scores.get(r.series.id, 0),
                r.sale_count is None,
                -(r.sale_count or 0),
            )
    return sorted(rows, key=key)


@router.get("/vehicles/{series_id}", response_model=VehicleDetailOut)
def vehicle_detail(series_id: int, db: Session = Depends(get_session)) -> VehicleDetailOut:
    series = catalog.get_series(db, series_id)
    if series is None:
        raise not_found(f"车型系列不存在：{series_id}")

    price_min, price_max = catalog.series_price_range(db, series_id)
    sales = catalog.latest_sales(db, series_id)
    years = catalog.series_model_years(db, series_id)
    brand = catalog.brand_of_series(db, series)
    source_page_url, source_name = catalog.series_source_page(db, series_id)
    # 唯一跳转入口：官方车型页优先；官方缺失时回退数据来源页（如实标注来源，不冒充官网）
    external_link: ExternalLinkOut | None = None
    if series.official_page_url:
        external_link = ExternalLinkOut(kind="official", url=series.official_page_url)
    elif source_page_url:
        external_link = ExternalLinkOut(kind="source", url=source_page_url, source_name=source_name)

    return VehicleDetailOut(
        id=series.id,
        name=series.name,
        aliases=series.aliases or [],
        brand=BrandRef(id=brand.id, name=brand.name) if brand else None,
        body_type=series.body_type,
        positioning=series.positioning,
        energy_types=series.energy_types or [],
        official_page_url=series.official_page_url,
        thumbnail_url=proxy_image_url(series.thumbnail_url),
        active_status=series.active_status,
        price_range=PriceRange(min=float(price_min) if price_min is not None else None,
                               max=float(price_max) if price_max is not None else None),
        latest_sales=LatestSales(
            month=sales.month if sales else None,
            sales_count=sales.sales_count if sales else None,
            sales_type=sales.sales_type if sales else None,
            source_name=sales.source.name if sales and sales.source else None,
        ),
        model_years=[ModelYearOut(id=y.id, year_name=y.year_name, launch_status=y.launch_status) for y in years],
        data_updated_at=data_freshness(
            series_verified_at=series.last_verified_at,
            sales_verified_at=sales.last_verified_at if sales else None,
        ),
        price_range_note=series.price_range_note,
        external_link=external_link,
    )


@router.get("/vehicles/{series_id}/variants", response_model=list[VariantOut])
def vehicle_variants(
    series_id: int,
    model_year_id: int | None = Query(default=None),
    energy_type: Literal["BEV", "PHEV", "EREV", "HEV", "ICE"] | None = Query(default=None),
    db: Session = Depends(get_session),
) -> list[VariantOut]:
    if catalog.get_series(db, series_id) is None:
        raise not_found(f"车型系列不存在：{series_id}")

    variants = catalog.series_variants(db, series_id, model_year_id=model_year_id, energy_type=energy_type)
    years = {y.id: y.year_name for y in catalog.series_model_years(db, series_id)}
    return [_variant_out(db, v, years.get(v.model_year_id)) for v in variants]


def _variant_out(db: Session, v: VehicleVariant, year_name: str | None) -> VariantOut:
    price = catalog.variant_current_price(db, v.id)
    facts = []
    for f in catalog.variant_facts(db, v.id):
        raw = f.fact_value
        value = raw or MISSING_VALUE_LABEL
        display = display_fact_value(raw, f.unit, f.cycle) or MISSING_VALUE_LABEL
        facts.append(
            SpecFactOut(
                category=f.category,
                fact_key=f.fact_key,
                label=fact_display_label(f.fact_key, f.unit, f.cycle),
                value=value,
                unit=f.unit,
                cycle=f.cycle,
                display=display,
            )
        )
    return VariantOut(
        id=v.id,
        series_id=v.series_id,
        model_year_id=v.model_year_id,
        year_name=year_name,
        display_name=v.display_name,
        config_version=v.config_version,
        powertrain=v.powertrain,
        drivetrain=v.drivetrain,
        package=v.package,
        energy_type=v.energy_type,
        body_type=v.body_type,
        status=v.status,
        official_price=(
            VariantPriceOut(
                price_cny=float(price.price_cny),
                price_type=price.price_type,
                effective_from=price.effective_from,
                effective_to=price.effective_to,
            )
            if price
            else None
        ),
        spec_facts=facts,
    )
