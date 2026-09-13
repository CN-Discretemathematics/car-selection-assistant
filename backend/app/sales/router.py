"""首页接口：GET /home。

按指定月份（默认最近完整自然月）的车型销量排序，
支持能源/车身/价格/品牌类别筛选，以及关键词搜索（车系名 / 品牌名 / 别名）。

返回**榜单前 N 名**（limit，默认 20）：完整榜单由 /vehicles 承担（可按销量排序浏览），
命中总数通过 `X-Total-Count` 响应头返回——2026-09 补齐销量数据后单月有 650 个车系，
首页若整体返回会把 HTML 撑到数 MB（实测 4.7MB），故在此限流。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.catalog import services as catalog
from app.catalog.series_index import keyword_needle, keyword_score
from app.common.database import get_session
from app.common.enums import NEW_ENERGY_TYPES
from app.common.images import proxy_image_url
from app.common.models import Brand, MonthlySales, OfficialPrice, VehicleSeries, VehicleVariant
from app.sales.schemas import HomeCardOut, PriceRangeOut, SalesSourceOut

router = APIRouter(tags=["home"])
HOME_DEFAULT_LIMIT = 20
HOME_MAX_LIMIT = 100


@router.get("/home", response_model=list[HomeCardOut])
def home(
    response: Response,
    q: str | None = Query(
        default=None,
        max_length=40,
        description="关键词：车系名 / 品牌名 / 别名（忽略大小写与分隔符），与 /vehicles 同口径",
    ),
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM；默认最近完整自然月"),
    body_type: str | None = Query(default=None),
    energy_type: str | None = Query(default=None, description="new_energy / fuel，或具体 energy_type 枚举"),
    price_min: float | None = Query(default=None, ge=0),
    price_max: float | None = Query(default=None, ge=0),
    brand_type: str | None = Query(default=None),
    sort: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(
        default=HOME_DEFAULT_LIMIT,
        ge=1,
        le=HOME_MAX_LIMIT,
        description="榜单条数上限（命中总数见 X-Total-Count 响应头）",
    ),
    db: Session = Depends(get_session),
) -> list[HomeCardOut]:
    # 默认月份 = 最近一个有销量数据的月份（销量数据月中发布，避免月初首页整体为空），
    # 显式传 month 时仍按指定月份查询
    target_month = month or catalog.latest_sales_month(db)

    stmt = (
        select(MonthlySales, VehicleSeries, Brand)
        .join(VehicleSeries, MonthlySales.series_id == VehicleSeries.id)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(
            MonthlySales.month == target_month,
            MonthlySales.sales_type.in_(("retail", "portal")),
            VehicleSeries.active_status == "active",
            Brand.active_status == "active",
        )
    )
    if body_type:
        stmt = stmt.where(VehicleSeries.body_type == body_type)
    if brand_type:
        stmt = stmt.where(Brand.brand_type == brand_type)

    order = MonthlySales.sales_count.desc() if sort == "desc" else MonthlySales.sales_count.asc()
    rows = db.execute(stmt.order_by(order)).all()
    # 口径优先级：逐车系零售优先、门户回退（评审 M2——某车系有零售数据时，
    # 不应因其他车系只有门户口径而从首页整体消失）
    chosen: dict[int, tuple[MonthlySales, VehicleSeries, Brand]] = {}
    for sales, series, brand in rows:
        cur = chosen.get(series.id)
        if cur is None or sales.sales_type == "retail":
            chosen[series.id] = (sales, series, brand)
    rows = list(chosen.values())
    # 排名始终按销量从高到低（评审 P2：升序查看时 rank=1 不应给销量最低者，
    # 否则首页第 1 名奖牌会戴在销量垫底车型上）
    rank_by_series = {
        sid: rank
        for rank, (sales, series, _brand) in enumerate(
            sorted(rows, key=lambda r: -r[0].sales_count), start=1
        )
        for sid in [series.id]
    }
    rows.sort(key=lambda r: r[0].sales_count, reverse=(sort == "desc"))

    # 批量：各车系当前指导价区间（评审 L8——与 catalog.series_price_range 同口径：
    # 在售款型 + official_msrp + 当前生效价）
    price_ranges: dict[int, tuple[float | None, float | None]] = {}
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

    matched: list[tuple[MonthlySales, VehicleSeries, Brand, float | None, float | None]] = []
    # 关键词与筛选同口径：排名保留全站真实名次（与能源/价格筛选一致，不重新编号）
    needle = keyword_needle(q)
    for sales, series, brand in rows:
        if needle and keyword_score(series, brand, needle) is None:
            continue
        if not _energy_matches(series.energy_types or [], energy_type):
            continue
        price_min_val, price_max_val = price_ranges.get(series.id, (None, None))
        if price_min is not None and (price_max_val is None or price_max_val < price_min):
            continue
        if price_max is not None and (price_min_val is None or price_min_val > price_max):
            continue
        matched.append((sales, series, brand, price_min_val, price_max_val))

    # 命中总数走响应头：首页只渲染前 limit 名（榜单语义），完整榜单在 /vehicles
    response.headers["X-Total-Count"] = str(len(matched))
    matched = matched[:limit]

    cards: list[HomeCardOut] = []
    for sales, series, brand, price_min_val, price_max_val in matched:
        cards.append(
            HomeCardOut(
                rank=rank_by_series.get(series.id, 0),
                series_id=series.id,
                series_name=series.name,
                brand_id=brand.id,
                brand_name=brand.name,
                thumbnail_url=proxy_image_url(series.thumbnail_url),
                body_type=series.body_type,
                energy_types=series.energy_types or [],
                month=sales.month,
                sales_count=sales.sales_count,
                sales_type=sales.sales_type,
                price_range=PriceRangeOut(
                    min=price_min_val,
                    max=price_max_val,
                ),
                source=SalesSourceOut(id=sales.source_id, name=sales.source.name if sales.source else None),
                data_updated_at=sales.last_verified_at or series.last_verified_at,
                price_range_note=series.price_range_note,
            )
        )
    return cards


def _energy_matches(series_energy_types: list[str], energy_filter: str | None) -> bool:
    """能源筛选（HEV 归燃油侧）。

    新能源侧 = 系列含任一新能源动力；燃油侧 = 系列含任一非新能源动力；
    多动力系列（如同时含 BEV 与 ICE）可同时出现在两侧。
    """
    if not energy_filter:
        return True
    if energy_filter == "new_energy":
        return any(t in NEW_ENERGY_TYPES for t in series_energy_types)
    if energy_filter == "fuel":
        return any(t not in NEW_ENERGY_TYPES for t in series_energy_types)
    return energy_filter in series_energy_types
