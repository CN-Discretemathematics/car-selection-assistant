"""车型列表的 SQL 下推（2026-10-21）。

## 为什么

**无界查询禁令**：禁止「全表载入后在 Python 里过滤分页」。

此前 `/vehicles` 把 908 个在售车系**全部**载进内存，再在 Python 里过滤/排序/切片，
还要为**全部**车系算一遍价格区间（≈9k 款型）与最新销量（当月销量全扫），
最后只展示 20 条。本机实测 **46 ms**（本地快照库）。

本模块把「过滤 + 价格区间 + 最新销量 + 排序 + 分页」整体下推到 SQL：**当页要什么算什么**。

## 口径不变（硬要求）

- 过滤口径与旧实现**逐条一致**（品牌类别 / 车身 / 能源分桶 / 价格区间）；
- 排序口径与旧实现**逐条一致**（销量倒序 / 价格升降序，含「无销量、无价格排最后」）；
- **关键词相关度排序仍由 `keyword_score` 独占**（Python 侧）——
  本模块**不重写评分口径**（那是漂移的最大来源），只做「过滤 + 取数」；
  关键词路径返回**全部**过滤结果，由 Python 侧打分排序后分页
  （那条路径的结果集本来就小，且它才是「搜得到与问得到一致」的地方）。

`tests/test_vehicles_sql_pushdown.py` 用**旧实现做 oracle**逐条比对（等价性回归）。

## 双栈语义（SQLite / PostgreSQL）

- 能源类型是 JSON 列，过滤用**引号定界的 LIKE**（`%"BEV"%`）——
  两种数据库的 JSON 文本都是 `["BEV", "PHEV"]` 这种形态，元素被引号包着，
  故 `%"BEV"%` 只命中「正好是 BEV 的元素」✓（不会误命中 `BEVX`）。
- 「燃油」的语义是「存在某个非新能源类型」（能源枚举共 5 值，新能源 = BEV/PHEV/EREV），
  故等价于「非空且不含全部三种新能源」✓。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import String, case, func, literal, or_, select
from sqlalchemy.orm import Session

from app.catalog import services as catalog
from app.common.enums import ENERGY_TYPES, NEW_ENERGY_TYPES
from app.common.models import Brand, MonthlySales, OfficialPrice, Source, VehicleSeries, VehicleVariant

SortKey = Literal["sales_desc", "price_asc", "price_desc"]


@dataclass
class SeriesRow:
    """列表页的一行（车系 + 品牌 + 价格区间 + 最新销量）。"""

    series: VehicleSeries
    brand: Brand
    pmin: float | None
    pmax: float | None
    sale_month: str | None = None
    sale_count: int | None = None
    sale_type: str | None = None
    sale_source_name: str | None = None
    source_name: str | None = None
    #: 销量记录**自己**的核验日（卡片「更新」口径用它，见 `app/common/freshness.py`）
    sale_verified_at: datetime | None = None
    #: 过滤后的总行数（窗口函数一次算完）
    total: int = 0


def _energy_text():
    """能源类型 JSON 列的文本形态（两栈一致：`["BEV", "PHEV"]`）。"""
    return func.cast(VehicleSeries.energy_types, String)


def _energy_filter(energy_type: str | None):
    """能源过滤（口径与旧实现逐条一致，见模块 docstring）。

    ⚠️ 「燃油」的口径是「**存在某个非新能源类型**」（能源枚举共 5 值，新能源 =
    BEV/PHEV/EREV，故非新能源 = HEV/ICE），**不是**「不含全部三种新能源」——
    后者会把纯电车（只含 BEV）也算成燃油（实测：等价性回归当场抓出这个漂移）。
    """
    if not energy_type:
        return None
    text = _energy_text()
    if energy_type == "new_energy":
        return or_(*[text.like(f'%"{t}"%') for t in NEW_ENERGY_TYPES])
    if energy_type == "fuel":
        fuel_types = [t for t in ENERGY_TYPES if t not in NEW_ENERGY_TYPES]
        return or_(*[text.like(f'%"{t}"%') for t in fuel_types])
    return text.like(f'%"{energy_type}"%')


def _price_range_cte():
    """各车系当前指导价区间（在售款型 + official_msrp + 当前生效价）。"""
    return (
        select(
            VehicleVariant.series_id.label("series_id"),
            func.min(OfficialPrice.price_cny).label("pmin"),
            func.max(OfficialPrice.price_cny).label("pmax"),
        )
        .join(OfficialPrice, OfficialPrice.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.status == "on_sale",
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
        .group_by(VehicleVariant.series_id)
        .cte("price_range")
    )


def _latest_sales_cte(month: str | None):
    """各车系最新销量（数据月内零售优先、门户回退；口径与 /home 一致）。"""
    if not month:
        return None
    ranked = (
        select(
            MonthlySales.series_id.label("series_id"),
            MonthlySales.month.label("month"),
            MonthlySales.sales_count.label("sales_count"),
            MonthlySales.sales_type.label("sales_type"),
            MonthlySales.source_id.label("source_id"),
            # 销量记录**自己**的核验日——卡片「更新」口径要用它
            # （否则列表页显示的是车系的核验日，会早于它显示的销量月份 ⇒ 自相矛盾）。
            MonthlySales.last_verified_at.label("last_verified_at"),
            func.row_number()
            .over(
                partition_by=MonthlySales.series_id,
                order_by=(
                    case((MonthlySales.sales_type == "retail", 0), else_=1),
                    MonthlySales.id.desc(),
                ),
            )
            .label("rn"),
        )
        .where(
            MonthlySales.month == month,
            MonthlySales.sales_type.in_(("retail", "portal")),
        )
        .cte("ranked_sales")
    )
    return (
        select(
            ranked.c.series_id.label("series_id"),
            ranked.c.month.label("month"),
            ranked.c.sales_count.label("sales_count"),
            ranked.c.sales_type.label("sales_type"),
            ranked.c.source_id.label("source_id"),
            ranked.c.last_verified_at.label("last_verified_at"),
        )
        .where(ranked.c.rn == 1)
        .cte("latest_sales")
    )


def _order_by(sort: SortKey, price, sales):
    """排序（口径与旧实现逐条一致；末尾按 id 稳定——旧实现的并列顺序取决于
    查询返回顺序，那是不确定的，钉死 id 才可复现）。"""
    if sort == "price_asc":
        return (price.c.pmin.is_(None), price.c.pmin.asc(), VehicleSeries.id.asc())
    if sort == "price_desc":
        return (price.c.pmax.is_(None), price.c.pmax.desc(), VehicleSeries.id.asc())
    if sales is None:
        return (VehicleSeries.id.asc(),)
    return (
        sales.c.sales_count.is_(None),
        sales.c.sales_count.desc(),
        VehicleSeries.id.asc(),
    )


def list_series(
    db: Session,
    *,
    brand_type: str | None = None,
    body_type: str | None = None,
    energy_type: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    sort: SortKey = "sales_desc",
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
) -> tuple[list[SeriesRow], int, int]:
    """过滤 + 聚合 + 排序 + 分页，全部在 SQL 里。

    返回 (当页行, 过滤后的总行数, **钳制后**的页码)。

    `keyword` 非空时**不**分页：相关度排序由 `keyword_score` 独占（Python 侧），
    这里只负责把过滤后的候选集取出来。调用方据此在 Python 侧打分、排序、切片，
    并用候选集长度作为总数。
    """
    price = _price_range_cte()
    sales = _latest_sales_cte(catalog.latest_sales_month(db))

    # 价格区间只有三处要用：价格过滤、价格排序、**展示**。
    # 前两处要对全体候选算（无法下推），展示只需要**当页**那几行——
    # 于是默认路径（销量倒序、无价格过滤）不必为 908 个车系全量聚合
    # （≈9k 款型），改成当页补算（见 _price_ranges_for）。
    need_price_all = (
        price_min is not None
        or price_max is not None
        or sort in ("price_asc", "price_desc")
    )
    if not need_price_all:
        price = None

    stmt = select(
        VehicleSeries,
        Brand,
        price.c.pmin.label("pmin") if price is not None else literal(None).label("pmin"),
        price.c.pmax.label("pmax") if price is not None else literal(None).label("pmax"),
        sales.c.month.label("sale_month") if sales is not None else literal(None).label("sale_month"),
        sales.c.sales_count.label("sale_count")
        if sales is not None
        else literal(None).label("sale_count"),
        sales.c.sales_type.label("sale_type")
        if sales is not None
        else literal(None).label("sale_type"),
        sales.c.source_id.label("sale_source_id")
        if sales is not None
        else literal(None).label("sale_source_id"),
        sales.c.last_verified_at.label("sale_verified_at")
        if sales is not None
        else literal(None).label("sale_verified_at"),
        # 总数用窗口函数一次算完——另跑一条 COUNT 会把整条聚合（价格区间 +
        # 销量窗口）**再算一遍**，默认路径实测直接翻倍。
        func.count().over().label("total"),
    ).join(Brand, VehicleSeries.brand_id == Brand.id)
    if price is not None:
        stmt = stmt.outerjoin(price, price.c.series_id == VehicleSeries.id)
    if sales is not None:
        stmt = stmt.outerjoin(sales, sales.c.series_id == VehicleSeries.id)
    stmt = stmt.where(VehicleSeries.active_status == "active")
    if brand_type:
        stmt = stmt.where(Brand.brand_type == brand_type)
    if body_type:
        stmt = stmt.where(VehicleSeries.body_type == body_type)
    energy = _energy_filter(energy_type)
    if energy is not None:
        stmt = stmt.where(energy)
    if price_min is not None:
        # 旧口径：有价区间的车系，其**最高价**必须 ≥ price_min（无价格区间的排除）
        stmt = stmt.where(price.c.pmax.is_not(None), price.c.pmax >= price_min)
    if price_max is not None:
        stmt = stmt.where(price.c.pmin.is_not(None), price.c.pmin <= price_max)

    if keyword:
        rows = _fetch(db, stmt, price, sales, sort, None, None)
        _fill_price_ranges(db, rows, price)
        return rows, len(rows), page

    rows = _fetch(db, stmt, price, sales, sort, page_size, (page - 1) * page_size)
    if rows:
        total = int(rows[0].total)
        total_pages = max((total + page_size - 1) // page_size, 1)
        # 越界页码钳制到最后一页（评审 P2：page=999 直接空列表不友好）
        if page > total_pages:
            page = total_pages
            rows = _fetch(db, stmt, price, sales, sort, page_size, (page - 1) * page_size)
        _fill_price_ranges(db, rows, price)
        return rows, total, page

    # 空页：窗口函数拿不到总数（没行）→ 退回到单独 COUNT（此时结果集本就小）
    total = (
        db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar()
        or 0
    )
    total_pages = max((total + page_size - 1) // page_size, 1)
    if page > total_pages:
        page = total_pages
        rows = _fetch(db, stmt, price, sales, sort, page_size, (page - 1) * page_size)
        _fill_price_ranges(db, rows, price)
    return rows, total, page


def _fill_price_ranges(db, rows, price) -> None:
    """当页补算价格区间（默认路径：价格区间只对**当页**算，不为 908 个车系全量聚合）。"""
    if price is not None:
        return  # 主查询已经带了价格区间
    ids = [r.series.id for r in rows]
    if not ids:
        return
    ranges = _price_ranges_for(db, ids)
    for row in rows:
        pmin, pmax = ranges.get(row.series.id, (None, None))
        row.pmin, row.pmax = pmin, pmax


def _price_ranges_for(db, series_ids: list[int]) -> dict[int, tuple[float, float]]:
    """指定车系的当前指导价区间（批量，一条查询）。"""
    out: dict[int, tuple[float, float]] = {}
    if not series_ids:
        return out
    for sid, pmin, pmax in db.execute(
        select(
            VehicleVariant.series_id,
            func.min(OfficialPrice.price_cny),
            func.max(OfficialPrice.price_cny),
        )
        .join(OfficialPrice, OfficialPrice.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.series_id.in_(series_ids),
            VehicleVariant.status == "on_sale",
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
        .group_by(VehicleVariant.series_id)
    ).all():
        out[int(sid)] = (float(pmin), float(pmax))
    return out


def _fetch(db, stmt, price, sales, sort, limit: int | None, offset: int | None):
    """执行查询并装配行；来源名批量取（避免逐行 N+1）。"""
    q = stmt.order_by(*_order_by(sort, price, sales))
    if limit is not None:
        q = q.limit(limit).offset(offset or 0)
    raw = db.execute(q).all()

    source_ids = {r[0].source_id for r in raw if r[0].source_id} | {
        r[7] for r in raw if r[7]
    }
    source_names = {}
    if source_ids:
        source_names = {
            s.id: s.name
            for s in db.scalars(select(Source).where(Source.id.in_(source_ids))).all()
        }
    rows: list[SeriesRow] = []
    for series, brand, pmin, pmax, month, count, stype, sale_source_id, sale_verified, total in raw:
        rows.append(
            SeriesRow(
                series=series,
                brand=brand,
                pmin=float(pmin) if pmin is not None else None,
                pmax=float(pmax) if pmax is not None else None,
                sale_month=month,
                sale_count=count,
                sale_type=stype,
                sale_source_name=(
                    source_names.get(sale_source_id) if sale_source_id else None
                ),
                source_name=source_names.get(series.source_id) if series.source_id else None,
                sale_verified_at=sale_verified,
                total=int(total or 0),
            )
        )
    return rows
