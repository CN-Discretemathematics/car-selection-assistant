"""车型列表 / 详情 / SKU 列表接口。

- GET /vehicles                 全部车型浏览（筛选/排序/分页）
- GET /vehicles/{series_id}
- GET /vehicles/{series_id}/variants
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.catalog import services as catalog
from app.catalog.series_index import normalize_name
from app.common.database import get_session
from app.common.enums import MISSING_VALUE_LABEL, NEW_ENERGY_TYPES
from app.common.errors import not_found
from app.common.images import proxy_image_url
from app.common.models import Brand, MonthlySales, OfficialPrice, Source, VehicleSeries, VehicleVariant
from app.vehicles.schemas import (
    BrandRef,
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


def keyword_score(series: VehicleSeries, brand: Brand, needle: str) -> int | None:
    """关键词匹配得分：越小越靠前；不匹配返回 None。

    归一化后比较（去空白/分隔符 + 小写），因此「腾势Z9 GT」「z9gt」「Z9GT」等效。
    0 = 车系名/别名完全相同；1 = 车系名前缀，或品牌名精确命中（列出该品牌全部车系）；
    2 = 子串命中（车系名/别名/品牌名的任意位置）。
    """
    if not needle:
        return None
    brand_names = [brand.name, *(brand.aliases or [])]
    best: int | None = None
    for brand_name in brand_names:
        normalized = normalize_name(brand_name or "")
        if not normalized:
            continue
        if normalized == needle:
            best = 1 if best is None else min(best, 1)
        elif needle in normalized:
            best = 2 if best is None else min(best, 2)
    names = [series.name, *(series.aliases or [])]
    brand_name = brand.name or ""
    if brand_name and series.name.startswith(brand_name):
        # 车系名自带品牌前缀时，额外登记「去掉品牌名」的短名（海豚 → 比亚迪海豚）
        names.append(series.name[len(brand_name) :])
    for name in names:
        normalized = normalize_name(name or "")
        if not normalized:
            continue
        if normalized == needle:
            score = 0
        elif normalized.startswith(needle):
            score = 1
        elif needle in normalized:
            score = 2
        else:
            continue
        best = score if best is None else min(best, score)
    return best


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
    rows = db.execute(
        select(VehicleSeries, Brand)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(VehicleSeries.active_status == "active")
    ).all()

    source_names = {
        s.id: s.name
        for s in db.scalars(
            select(Source).where(Source.id.in_({r.source_id for r, _ in rows if r.source_id}))
        ).all()
    }

    # 批量：各车系当前指导价区间（避免逐车系 N+1）
    # 口径与 /home、详情页一致：在售款型 + official_msrp + 当前生效价（评审 P1）
    price_ranges: dict[int, tuple[float, float]] = {}
    for series_id, pmin, pmax in db.execute(
        select(
            VehicleVariant.series_id,
            func.min(OfficialPrice.price_cny),
            func.max(OfficialPrice.price_cny),
        )
        .join(OfficialPrice, OfficialPrice.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.status == "on_sale",
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
        .group_by(VehicleVariant.series_id)
    ).all():
        price_ranges[series_id] = (float(pmin), float(pmax))

    # 批量：各车系最新销量（口径与 /home 一致：最新数据月内零售优先、门户回退，评审 P2）
    latest_sales: dict[int, MonthlySales] = {}
    latest_month = catalog.latest_sales_month(db)
    for sale in db.scalars(
        select(MonthlySales)
        .where(
            MonthlySales.month == latest_month,
            MonthlySales.sales_type.in_(("retail", "portal")),
        )
        .order_by(
            case((MonthlySales.sales_type == "retail", 0), else_=1),
            MonthlySales.id.desc(),
        )
    ):
        if sale.series_id not in latest_sales:
            latest_sales[sale.series_id] = sale

    items: list[tuple[VehicleSeries, Brand, float | None, float | None, MonthlySales | None]] = []
    for series, brand in rows:
        pmin, pmax = price_ranges.get(series.id, (None, None))
        if price_min is not None and (pmax is None or pmax < price_min):
            continue
        if price_max is not None and (pmin is None or pmin > price_max):
            continue
        if brand_type and brand.brand_type != brand_type:
            continue
        if body_type and series.body_type != body_type:
            continue
        if energy_type:
            types = set(series.energy_types or [])
            if energy_type == "new_energy":
                if not (types & set(NEW_ENERGY_TYPES)):
                    continue
            elif energy_type == "fuel":
                if types and not (types - set(NEW_ENERGY_TYPES)):
                    continue
                if not types:
                    continue
            elif energy_type not in types:
                continue
        items.append((series, brand, pmin, pmax, latest_sales.get(series.id)))

    # 关键词过滤：命中即按相关度（精确 > 前缀/品牌 > 子串）优先，再按用户选择的排序
    scores: dict[int, int] = {}
    needle = normalize_name(q) if q and q.strip() else ""
    if needle:
        matched = []
        for series, brand, pmin, pmax, sale in items:
            score = keyword_score(series, brand, needle)
            if score is None:
                continue
            scores[series.id] = score
            matched.append((series, brand, pmin, pmax, sale))
        items = matched

    if sort == "price_asc":
        items.sort(key=lambda x: (scores.get(x[0].id, 0), x[2] is None, x[2] or 0))
    elif sort == "price_desc":
        items.sort(key=lambda x: (scores.get(x[0].id, 0), x[3] is None, -(x[3] or 0)))
    else:  # sales_desc
        items.sort(
            key=lambda x: (scores.get(x[0].id, 0), x[4] is None, -(x[4].sales_count if x[4] else 0))
        )

    total = len(items)
    total_pages = max((total + page_size - 1) // page_size, 1)
    # 越界页码钳制到最后一页（评审 P2：page=999 直接空列表不友好）
    if page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]

    out_items: list[VehicleListItemOut] = []
    for series, brand, pmin, pmax, sale in page_items:
        out_items.append(
            VehicleListItemOut(
                series_id=series.id,
                series_name=series.name,
                brand_id=brand.id,
                brand_name=brand.name,
                brand_type=brand.brand_type,
                body_type=series.body_type,
                energy_types=series.energy_types or [],
                thumbnail_url=proxy_image_url(series.thumbnail_url),
                price_range=PriceRange(min=pmin, max=pmax),
                price_range_note=series.price_range_note,
                latest_sales=LatestSales(
                    month=sale.month if sale else None,
                    sales_count=sale.sales_count if sale else None,
                    sales_type=sale.sales_type if sale else None,
                    source_name=sale.source.name if sale and sale.source else None,
                ),
                source_name=source_names.get(series.source_id),
                data_updated_at=series.last_verified_at,
            )
        )
    return VehicleListOut(total=total, page=page, page_size=page_size, items=out_items)


@router.get("/vehicles/{series_id}", response_model=VehicleDetailOut)
def vehicle_detail(series_id: int, db: Session = Depends(get_session)) -> VehicleDetailOut:
    series = catalog.get_series(db, series_id)
    if series is None:
        raise not_found(f"车型系列不存在：{series_id}")

    price_min, price_max = catalog.series_price_range(db, series_id)
    sales = catalog.latest_sales(db, series_id)
    years = catalog.series_model_years(db, series_id)
    brand = catalog.brand_of_series(db, series)

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
        data_updated_at=series.last_verified_at,
        price_range_note=series.price_range_note,
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
