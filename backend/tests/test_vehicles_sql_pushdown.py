"""`/vehicles` 的 SQL 下推：与旧实现**逐条等价**（2026-10-21）。

R7.2（engineering-standards）禁止「全表载入后在 Python 里过滤分页」。
本笔把过滤 / 聚合 / 排序 / 分页下推到 SQL（`app/vehicles/listing.py`），
于是必须证明**口径没变**——否则列表页会静默换一批车。

做法：**旧实现当 oracle**（`_legacy_list` 是下推前的原逻辑的等价副本），
在一组种子数据 + 一张查询矩阵上逐条比对「每页的车系 id 序列」与「总数」。

关键词的相关度排序本来就由 `keyword_score` 独占（两侧共用），
本文件真正比的是**过滤口径**与**分页边界**。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.catalog import services as catalog
from app.catalog.series_index import keyword_needle, keyword_score
from app.common.enums import NEW_ENERGY_TYPES
from app.common.models import Brand, MonthlySales, OfficialPrice, VehicleSeries, VehicleVariant
from app.vehicles.router import vehicle_list
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db_session: Session) -> None:
    """一小撮**刻意刁钻**的数据：同价不同品牌、空别名、只有停售款的车系、
    新能源/燃油混合、两个数据月（测「最新月」口径）。"""
    source = make_source(db_session, name="列表测试来源")
    brands: dict[str, Brand] = {}
    plan = [
        # (品牌名, 品牌类别, 车系名, 车身, 能源, 价格, 销量月, 销量)
        ("甲品牌", "domestic_nev", "甲车系", "sedan", ("BEV",), "150000", "2026-06", 120),
        ("甲品牌", "domestic_nev", "甲车系二代", "suv", ("BEV",), "180000", "2026-07", 300),
        ("乙品牌", "luxury", "乙车系", "suv", ("PHEV",), "320000", "2026-07", 90),
        ("乙品牌", "luxury", "乙车系二代", "mpv", ("ICE",), "260000", "2026-07", 45),
        ("丙品牌", "japanese", "丙车系", "sedan", ("HEV",), "120000", "2026-06", 10),
        ("丁品牌", "other_fuel", "丁车系", "pickup", ("ICE",), "90000", "2026-07", 5),
    ]
    for brand_name, brand_type, series_name, body, energy, price, month, count in plan:
        brand = brands.get(brand_name)
        if brand is None:
            brand = make_brand(db_session, name=brand_name, source=source)
            brand.brand_type = brand_type
            brands[brand_name] = brand
        series = make_series(
            db_session, brand, name=series_name, body_type=body,
            energy_types=energy, source=source,
        )
        year = make_year(db_session, series)
        variant = make_variant(
            db_session, series, year, config_version="标准版",
            energy_type=energy[0], price_cny=price, source=source,
        )
        assert variant.id  # 款型已入库（价格区间由它决定）
        sale = MonthlySales(
            series_id=series.id, month=month, sales_type="retail",
            sales_count=count, source_id=source.id,
        )
        db_session.add(sale)
    # 别名（关键词搜索要能命中别名）
    series = db_session.query(VehicleSeries).filter(VehicleSeries.name == "甲车系").one()
    series.aliases = ["甲别名"]
    # 一个**价格区间跨阈值**的车系（15 万 ~ 25 万）：
    # 「price_min=20 万」时旧口径按**最高价**判（250000 ≥ 200000 → 保留），
    # 若 SQL 误用最低价（150000 ≥ 200000 → 排除）就会漂——夹具必须能分辨这两个方向。
    wide_brand = make_brand(db_session, name="己品牌", source=source)
    wide_series = make_series(
        db_session, wide_brand, name="己车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    wide_year = make_year(db_session, wide_series)
    for price in ("150000", "250000"):
        make_variant(
            db_session, wide_series, wide_year, config_version=f"档{price}",
            energy_type="BEV", price_cny=price, source=source,
        )
    # 一个**没有在售款型**的车系（价格区间为空 → 价格过滤下必须被排除）
    ghost_brand = make_brand(db_session, name="戊品牌", source=source)
    make_series(
        db_session, ghost_brand, name="戊车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    db_session.commit()


def _legacy_list(db: Session, **kwargs) -> tuple[list[int], int]:
    """下推前的旧实现（等价副本）——**oracle**。"""
    q = kwargs.get("q")
    brand_type = kwargs.get("brand_type")
    body_type = kwargs.get("body_type")
    energy_type = kwargs.get("energy_type")
    price_min = kwargs.get("price_min")
    price_max = kwargs.get("price_max")
    sort = kwargs.get("sort", "sales_desc")
    page = kwargs.get("page", 1)
    page_size = kwargs.get("page_size", 20)

    rows = db.execute(
        __import__("sqlalchemy").select(VehicleSeries, Brand)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(VehicleSeries.active_status == "active")
    ).all()

    price_ranges: dict[int, tuple[float, float]] = {}
    for series_id, pmin, pmax in db.execute(
        __import__("sqlalchemy").select(
            VehicleVariant.series_id,
            __import__("sqlalchemy").func.min(OfficialPrice.price_cny),
            __import__("sqlalchemy").func.max(OfficialPrice.price_cny),
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

    latest_sales: dict[int, MonthlySales] = {}
    latest_month = catalog.latest_sales_month(db)
    for sale in db.scalars(
        __import__("sqlalchemy").select(MonthlySales)
        .where(
            MonthlySales.month == latest_month,
            MonthlySales.sales_type.in_(("retail", "portal")),
        )
        .order_by(
            __import__("sqlalchemy").case((MonthlySales.sales_type == "retail", 0), else_=1),
            MonthlySales.id.desc(),
        )
    ):
        if sale.series_id not in latest_sales:
            latest_sales[sale.series_id] = sale

    items = []
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

    scores: dict[int, int] = {}
    needle = keyword_needle(q)
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
    else:
        items.sort(
            key=lambda x: (scores.get(x[0].id, 0), x[4] is None, -(x[4].sales_count if x[4] else 0))
        )

    total = len(items)
    total_pages = max((total + page_size - 1) // page_size, 1)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]
    return [s.id for s, _b, _lo, _hi, _sale in page_items], total


def _call(db: Session, **kwargs) -> tuple[list[int], int]:
    args = {
        "q": None,
        "brand_type": None,
        "body_type": None,
        "energy_type": None,
        "price_min": None,
        "price_max": None,
        "sort": "sales_desc",
        "page": 1,
        "page_size": 20,
    }
    args.update(kwargs)
    out = vehicle_list(**args, db=db)
    return [v.series_id for v in out.items], out.total


MATRIX: list[dict] = [
    {},
    {"sort": "price_asc"},
    {"sort": "price_desc"},
    {"page": 2, "page_size": 2},
    {"page": 999, "page_size": 2},
    {"q": "甲"},
    {"q": "甲别名"},
    {"q": "乙"},
    {"brand_type": "domestic_nev"},
    {"body_type": "suv"},
    {"energy_type": "BEV"},
    {"energy_type": "new_energy"},
    {"energy_type": "fuel"},
    {"price_min": 200000},
    {"price_max": 200000},
    {"price_min": 100000, "price_max": 200000, "sort": "price_asc"},
    {"energy_type": "new_energy", "sort": "price_desc", "page_size": 3},
    {"q": "甲", "body_type": "suv"},
]


def test_sql_pushdown_matches_the_legacy_implementation(db_session: Session):
    """下推后的结果必须与旧实现**逐条一致**（每页的车系 id 序列 + 总数）。"""
    _seed(db_session)
    for kwargs in MATRIX:
        legacy_ids, legacy_total = _legacy_list(db_session, **kwargs)
        new_ids, new_total = _call(db_session, **kwargs)
        assert new_ids == legacy_ids, f"口径漂移：{kwargs}\n新={new_ids}\n旧={legacy_ids}"
        assert new_total == legacy_total, f"总数不一致：{kwargs} 新={new_total} 旧={legacy_total}"


def test_page_is_clamped_to_the_last_page(db_session: Session):
    """越界页码钳制到最后一页（评审 P2：page=999 直接空列表不友好）。"""
    _seed(db_session)
    out = vehicle_list(db=db_session, page=999, page_size=2, q=None, brand_type=None,
                       body_type=None, energy_type=None, price_min=None, price_max=None,
                       sort="sales_desc")
    assert out.page == out.total_pages if hasattr(out, "total_pages") else out.page >= 1
    assert out.items, "越界页码应钳制到最后一页而不是空列表"
