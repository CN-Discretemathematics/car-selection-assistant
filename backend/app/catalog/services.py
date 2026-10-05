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


def variants_current_prices(db: Session, variant_ids: list[int]) -> dict[int, OfficialPrice]:
    """批量取多款型的「当前生效官方指导价」，返回 {variant_id: OfficialPrice}。

    2026-10-02（P5.2 / H4）：`variant_current_price` 每次一条 SELECT，在
    「逐款型」的场景里就是 N+1（一个 8 款型的车系 = 8 次往返）。
    本函数是它的批量版，口径与单条版**逐字一致**：
    `effective_to IS NULL` + `price_type == "official_msrp"`，
    同款型多条时取 `effective_from` 最新的一条。

    注意：`official_msrp` 不按 variant 唯一，理论上同一 variant 可能有多条当前价；
    排序 + 「只留第一条」的逻辑按 (variant_id, effective_from) 全局排序后取，
    与逐条调用 `.first()` 的结果相同。
    """
    if not variant_ids:
        return {}
    stmt = (
        select(OfficialPrice)
        .where(
            OfficialPrice.variant_id.in_(variant_ids),
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
        .order_by(OfficialPrice.variant_id, OfficialPrice.effective_from.desc())
    )
    out: dict[int, OfficialPrice] = {}
    for price in db.scalars(stmt):
        # 同 variant 的后续行（更旧的 effective_from）一律不覆盖
        out.setdefault(price.variant_id, price)
    return out


def variants_facts(db: Session, variant_ids: list[int]) -> dict[int, list[SpecFact]]:
    """批量取多款型的全部事实，返回 {variant_id: [SpecFact, ...]}。

    2026-10-02（P5.2 / H4）：`variant_facts` 每次一条 SELECT，逐款型调用即 N+1。
    排序口径与单条版一致（category, fact_key）。
    """
    if not variant_ids:
        return {}
    stmt = (
        select(SpecFact)
        .where(SpecFact.variant_id.in_(variant_ids))
        .order_by(SpecFact.variant_id, SpecFact.category, SpecFact.fact_key)
    )
    out: dict[int, list[SpecFact]] = {}
    for fact in db.scalars(stmt):
        out.setdefault(fact.variant_id, []).append(fact)
    return out


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


def sales_ranking(
    db: Session,
    *,
    month: str | None = None,
    limit: int = 10,
    body_type: str | None = None,
) -> list[tuple[MonthlySales, VehicleSeries, Brand]]:
    """按销量从高到低返回车系榜。

    口径与 `app/sales/router.py::home` **逐条对齐**（2026-10-05 新增）：
      - 月份默认取**最近一个有销量数据的月份**（销量月中发布，月初按「最近完整
        自然月」取会让榜单整体消失——2026-09-02 生产故障）；
      - 同一车系同时有零售与门户口径时，**零售优先**，门户回退（评审 M2）；
      - 只统计车系/品牌都 active 的记录；
      - 排名恒按销量从高到低，不受展示排序影响（评审 P2）。

    ⚠️ **这里是第二份实现，不是共用一份**（2026-10-05 独立审查 P1-2 更正）。
    本函数最初的 docstring 与提交信息都写着「与 /home 同一份实现、不另写一份查询」
    ——**那是错的**：`/home` 从不调用本函数，SQL 构造与零售优先去重在
    `router.py:56-81` 与此处各存一份，两边可以自由分叉。

    没有直接合并的原因是 `/home` 还带 `sort`/`brand_type`/`q`/energy/price 过滤与
    `X-Total-Count`，抽取会把首页的排名语义（升序查看时 rank 仍为 1）一起卷进来，
    属于需要单独拍板的重构。

    在那之前，**用测试把两份钉在一起**：
    `backend/tests/test_sales_ranking_matches_home.py` 会真的调用 `/home` 并逐行比对
    顺序与数值——审查用「让 /home 单边加过滤」「让 /home 单边丢零售优先」两种变异
    证明过，旧测试全绿（59 passed），即「同口径」此前**没有任何东西在守**。

    助手侧的「什么车卖得好 / 热门榜 / 销量排名」确定性回答直接用它——
    生产实测此前该问题落到 LLM 后被答成「销量数据不完整，没法给你准确的热门榜」，
    并凭记忆列举了三款都不是销冠的车（2026-10-05，见台账第三十八节）。
    """
    target_month = month or latest_sales_month(db)
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
    chosen: dict[int, tuple[MonthlySales, VehicleSeries, Brand]] = {}
    for sales, series, brand in db.execute(stmt.order_by(MonthlySales.sales_count.desc())).all():
        cur = chosen.get(series.id)
        if cur is None or sales.sales_type == "retail":
            chosen[series.id] = (sales, series, brand)
    rows = sorted(chosen.values(), key=lambda r: -r[0].sales_count)
    return rows[:limit] if limit > 0 else rows


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


def series_source_page(db: Session, series_id: int) -> tuple[str | None, str | None]:
    """车系的「数据来源」入口：返回 (来源页 URL, 来源名)。

    URL 由「来源名 + 外部车系 id」（`external_series_refs`）确定性拼出，只认已知模板；
    没有映射记录时返回 `(None, None)`；来源没有模板或外部 id 形态可疑时返回 `(None, 来源名)`。
    绝不猜 URL。官方车型页链接（`series.official_page_url`）存在时应优先展示官方链接。
    """
    from app.common.models import ExternalSeriesRef, Source
    from app.sources.page_urls import source_page_url

    rows = db.execute(
        select(Source.name, ExternalSeriesRef.external_id)
        .join(ExternalSeriesRef, ExternalSeriesRef.source_id == Source.id)
        .where(ExternalSeriesRef.series_id == series_id)
        # 车系改号后可能留下多条同来源映射，取最新一条（更可能仍然有效），
        # 并继续向后找第一条有模板的（模板缺失/无映射都不猜 URL）
        .order_by(ExternalSeriesRef.id.desc())
    ).all()
    fallback_name: str | None = None
    for name, external_id in rows:
        url = source_page_url(name, str(external_id))
        if url:
            return url, name
        fallback_name = fallback_name or name
    return None, fallback_name


def variant_facts(db: Session, variant_id: int) -> list[SpecFact]:
    stmt = (
        select(SpecFact)
        .where(SpecFact.variant_id == variant_id)
        .order_by(SpecFact.category, SpecFact.fact_key)
    )
    return list(db.scalars(stmt))
