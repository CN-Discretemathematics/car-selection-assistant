"""Agent 业务工具。

每个能力都是类型化工具，全部在 Agent Worker 内实现，只经过 catalog 服务层访问数据；
不直接访问 SQL、Milvus、对象存储或任意网络资源。LLM 只能调用这些工具，
工具的返回值是回答中车辆事实的唯一来源。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import UserProfile
from app.catalog import services as catalog
from app.common.enums import NEW_ENERGY_TYPES
from app.common.models import (
    Brand,
    MonthlySales,
    OfficialPrice,
    SpecFact,
    VehicleSeries,
    VehicleVariant,
)

# ── 工具 1：vehicle_search ────────────────────────────────────────────────────
def vehicle_search(
    db: Session,
    query: str | None = None,
    body_type: str | None = None,
    energy_type: str | None = None,
    brand_id: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """按条件查询在售车型系列与 SKU。"""
    stmt = select(VehicleSeries).where(VehicleSeries.active_status == "active")
    if query:
        like = f"%{query}%"
        stmt = stmt.where(VehicleSeries.name.like(like))
    if body_type:
        stmt = stmt.where(VehicleSeries.body_type == body_type)
    if brand_id:
        stmt = stmt.where(VehicleSeries.brand_id == brand_id)
    series_list = db.scalars(stmt.limit(limit)).all()

    result = []
    for series in series_list:
        brand = db.get(Brand, series.brand_id)
        price_min, price_max = catalog.series_price_range(db, series.id)
        energy_types = list(series.energy_types or [])
        if energy_type:
            if energy_type == "new_energy" and not any(t in NEW_ENERGY_TYPES for t in energy_types):
                continue
            if energy_type == "fuel" and not any(t not in NEW_ENERGY_TYPES for t in energy_types):
                continue
            if energy_type not in ("new_energy", "fuel") and energy_type not in energy_types:
                continue
        result.append(
            {
                "series_id": series.id,
                "series_name": series.name,
                "brand_name": brand.name if brand else None,
                "body_type": series.body_type,
                "energy_types": energy_types,
                "price_range": {
                    "min": float(price_min) if price_min is not None else None,
                    "max": float(price_max) if price_max is not None else None,
                },
                "official_page_url": series.official_page_url,
                "source_id": series.source_id,
            }
        )
    return result


# ── 工具 2：sales_search ──────────────────────────────────────────────────────
def sales_search(db: Session, month: str | None = None, limit: int = 20) -> list[dict]:
    """查询指定月份（默认最近有销量数据的月份）的车型销量排行。

    口径与首页一致：零售优先，无零售时回退门户榜单口径（评审 M4——当前数据
    为汽车之家门户口径，硬编码 retail 会永远返回空）。默认月份回退到库内
    最新数据月（销量数据月中发布，月初按「最近完整自然月」取数会整体为空）。
    """
    target = month or catalog.latest_sales_month(db)
    rows = db.execute(
        select(MonthlySales, VehicleSeries, Brand)
        .join(VehicleSeries, MonthlySales.series_id == VehicleSeries.id)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(
            MonthlySales.month == target,
            MonthlySales.sales_type.in_(("retail", "portal")),
            VehicleSeries.active_status == "active",  # 评审 P2：与首页一致，排除下架车系
            Brand.active_status == "active",
        )
        .order_by(MonthlySales.sales_count.desc())
    ).all()
    # 口径与首页一致：逐车系零售优先、门户回退
    chosen: dict[int, tuple[MonthlySales, VehicleSeries, Brand]] = {}
    for sales, series, brand in rows:
        cur = chosen.get(series.id)
        if cur is None or sales.sales_type == "retail":
            chosen[series.id] = (sales, series, brand)
    out_rows = sorted(chosen.values(), key=lambda r: r[0].sales_count, reverse=True)[:limit]
    return [
        {
            "series_id": series.id,
            "series_name": series.name,
            "brand_name": brand.name,
            "month": sales.month,
            "sales_count": sales.sales_count,
            "sales_type": sales.sales_type,
            "source_id": sales.source_id,
        }
        for sales, series, brand in out_rows
    ]


# ── 工具 3：vehicle_evidence ──────────────────────────────────────────────────
def vehicle_evidence(db: Session, variant_id: int) -> dict:
    """返回 SKU 的配置、价格与官方来源（回答中的事实依据）。"""
    variant = catalog.get_variant(db, variant_id)
    if variant is None:
        return {"variant_id": variant_id, "error": "SKU 不存在"}
    series = db.get(VehicleSeries, variant.series_id)
    brand = db.get(Brand, series.brand_id) if series else None
    price = catalog.variant_current_price(db, variant_id)
    facts = [
        {
            "category": f.category,
            "fact_key": f.fact_key,
            "value": f.fact_value,
            "unit": f.unit,
            "cycle": f.cycle,
            "source_id": f.source_id,
        }
        for f in catalog.variant_facts(db, variant_id)
    ]
    return {
        "variant_id": variant.id,
        "series_id": variant.series_id,
        "series_name": series.name if series else None,
        "brand_name": brand.name if brand else None,
        "display_name": variant.display_name,
        "energy_type": variant.energy_type,
        "official_price": float(price.price_cny) if price else None,
        "facts": facts,
        "official_page_url": series.official_page_url if series else None,
        "source_id": variant.source_id,
    }


# ── 工具 4：official_link_tool ────────────────────────────────────────────────
def official_link_tool(db: Session, series_id: int) -> dict:
    """返回经过验证的官方车型页链接。"""
    series = catalog.get_series(db, series_id)
    if series is None:
        return {"series_id": series_id, "error": "车型系列不存在"}
    return {
        "series_id": series.id,
        "series_name": series.name,
        "official_page_url": series.official_page_url,
    }


# ── 工具 5：comparison_tool ───────────────────────────────────────────────────
def comparison_tool(db: Session, variant_ids: list[int]) -> dict:
    """返回 SKU 对比数据（参数分组 + 相同参数列表）。"""
    from app.variants.normalization import fact_identity

    rows: list[dict] = []
    common: dict[tuple[str, str], list] = {}
    for variant_id in variant_ids:
        evidence = vehicle_evidence(db, variant_id)
        if "error" in evidence:
            continue
        rows.append(evidence)
    for i, row in enumerate(rows):
        for fact in row["facts"]:
            key = (fact["category"], fact["fact_key"])
            identity = fact_identity(fact["category"], fact["fact_key"], fact["value"] or "", fact["unit"], fact["cycle"])
            common.setdefault(key, []).append((i, identity))
    common_params = [
        {"category": k[0], "fact_key": k[1]}
        for k, values in common.items()
        if len(values) == len(rows) and len({v[1] for v in values}) == 1
    ]
    return {"variants": rows, "common_params": common_params}


# ── 工具 5b：retrieval_search（混合检索）────────────
def retrieval_search(
    db: Session,
    query: str,
    filters: dict | None = None,
    top_k: int = 5,
) -> list[dict]:
    """混合检索（LangGraph 查询流水线：BM25 ∥ 向量召回 → RRF 融合 → 重排 → 把关）。

    返回官方文档/结构化事实切片，作为 Agent 回答的证据补充；
    检索结果不产生新事实，只引用数据库中的既有内容。
    """
    from app.rag.service import search

    top_k = min(max(top_k, 1), 10)  # 边界收敛：1~10
    allowed_filter_keys = {"brand_id", "series_id", "model_year_id", "variant_id", "source_id", "energy_type"}
    safe_filters = {k: v for k, v in (filters or {}).items() if k in allowed_filter_keys}
    results = search(db, query, filters=safe_filters, top_k=top_k)
    return [
        {
            "chunk_id": r.chunk_id,
            "score": r.score,
            "text": r.text,
            "kind": r.kind,
            "series_id": r.series_id,
            "variant_id": r.variant_id,
            "source_id": r.source_id,
            "source_url": r.source_url,
        }
        for r in results
    ]


# ── 工具 6：recommendation_tool（硬约束 + 软评分）────────────────────────────
def _parse_number(value: str | None) -> float | None:
    if not value:
        return None
    m = re.search(r"\d+(?:\.\d+)?", value)
    return float(m.group(0)) if m else None


# 评分所需事实键（归一化键名 + 汽车之家真实配置表键名，双兼容）
_SEAT_FACT_KEYS = ("seats", "座位数", "座位数(个)", "座位数（个）")
_LENGTH_FACT_KEYS = ("length_mm", "长*宽*高(mm)", "长度(mm)", "车长(mm)")
_POWER_FACT_KEYS = ("power_kw", "电动机总功率(kW)", "发动机最大功率(kW)", "系统综合功率(kW)", "最大功率(kW)")
_COMFORT_CATEGORIES = ("舒适性", "内部配置", "外部配置")
_INTELLIGENCE_CATEGORIES = ("智能驾驶", "智能座舱", "智能/辅助驾驶")


def _extract_seats(facts: dict[tuple[str, str], dict]) -> float | None:
    for key in _SEAT_FACT_KEYS:
        value = facts.get(("座位数", key), {}).get("value") or facts.get(("参数信息", key), {}).get("value")
        if value is not None:
            return _parse_number(value)
    return None


def _extract_length(facts: dict[tuple[str, str], dict]) -> float | None:
    for key in _LENGTH_FACT_KEYS:
        value = facts.get(("尺寸", key), {}).get("value") or facts.get(("参数信息", key), {}).get("value")
        if value is not None:
            return _parse_number(value)
    return None


def _extract_power(facts: dict[tuple[str, str], dict]) -> float | None:
    for key in _POWER_FACT_KEYS:
        value = facts.get(("动力", key), {}).get("value") or facts.get(("参数信息", key), {}).get("value")
        if value is not None:
            return _parse_number(value)
    return None


def recommendation_tool(db: Session, profile: UserProfile, limit: int = 5) -> dict:
    """确定性推荐：PostgreSQL 硬条件筛选 + 软评分。

    事实全部来自数据库；评分维度无数据时不计分，禁止猜测。
    硬约束（在售/锁定车系/车身/能源/价格区间）全部下推 SQL（评审 P1：
    此前每次消息全量拉全部在售款型再 Python 过滤，大库下内存/延迟线性增长），
    剩余约束（座位数等基于事实的）在 Python 内完成。
    """
    DEFAULT_WEIGHTS = {
        "budget": 0.30,
        "usage": 0.15,
        "space": 0.10,
        "energy": 0.15,
        "power": 0.10,
        "comfort": 0.05,
        "intelligence": 0.05,
        "maintenance": 0.10,
    }
    weights = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (profile.weights or {}).items() if v is not None}}

    # ── 第一层：硬约束下推 SQL ────────────────────────────────────────────
    from sqlalchemy import or_

    stmt = select(VehicleVariant).where(VehicleVariant.status == "on_sale")
    # 会话锁定的车系（用户点名过、尚未解锁）
    if profile.locked_series_ids:
        stmt = stmt.where(VehicleVariant.series_id.in_(profile.locked_series_ids))
    # 车身类型偏好
    if profile.body_type:
        stmt = stmt.where(VehicleVariant.body_type.in_(profile.body_type))
    # 能源偏好（new_energy/fuel 泛化 + 具体类型）
    prefs = list(profile.energy_preference or [])
    if prefs:
        allowed = {e for e in prefs if e not in ("new_energy", "fuel")}
        if "new_energy" in prefs:
            allowed |= set(NEW_ENERGY_TYPES)
        if "fuel" in prefs:
            allowed |= {t for t in ("BEV", "PHEV", "EREV", "HEV", "ICE") if t not in NEW_ENERGY_TYPES}
        if allowed:
            stmt = stmt.where(VehicleVariant.energy_type.in_(sorted(allowed)))
    # 排除偏好（评审 L2）：能源与车身
    avoided = set(profile.avoid or [])
    energy_avoid = {e for e in avoided if e in ("BEV", "PHEV", "EREV", "HEV", "ICE")}
    if "new_energy" in avoided:
        energy_avoid |= set(NEW_ENERGY_TYPES)
    if "fuel" in avoided:
        energy_avoid |= {t for t in ("BEV", "PHEV", "EREV", "HEV", "ICE") if t not in NEW_ENERGY_TYPES}
    if energy_avoid:
        stmt = stmt.where(~VehicleVariant.energy_type.in_(sorted(energy_avoid)))
    body_avoid = {b for b in avoided if b in ("sedan", "suv", "mpv", "pickup")}
    if body_avoid:
        stmt = stmt.where(~VehicleVariant.body_type.in_(sorted(body_avoid)))
    # 当前生效指导价存在 + 预算区间
    price_cond = [
        OfficialPrice.effective_to.is_(None),
        OfficialPrice.price_type == "official_msrp",
    ]
    if profile.budget.min is not None:
        price_cond.append(OfficialPrice.price_cny >= profile.budget.min)
    if profile.budget.max is not None:
        price_cond.append(OfficialPrice.price_cny <= profile.budget.max)
    stmt = stmt.where(VehicleVariant.id.in_(select(OfficialPrice.variant_id).where(*price_cond)))

    variants = db.scalars(stmt).all()
    variant_ids = [v.id for v in variants]

    # 批量：当前指导价
    prices = {
        p.variant_id: float(p.price_cny)
        for p in db.scalars(
            select(OfficialPrice).where(
                OfficialPrice.variant_id.in_(variant_ids),
                OfficialPrice.effective_to.is_(None),
                OfficialPrice.price_type == "official_msrp",
            )
        ).all()
    } if variant_ids else {}

    # 批量：评分所需事实（只取相关键与类别，控制内存）
    fact_rows = (
        db.scalars(
            select(SpecFact).where(
                SpecFact.variant_id.in_(variant_ids),
                or_(
                    SpecFact.fact_key.in_(list(_SEAT_FACT_KEYS) + list(_LENGTH_FACT_KEYS) + list(_POWER_FACT_KEYS)),
                    SpecFact.category.in_(list(_COMFORT_CATEGORIES) + list(_INTELLIGENCE_CATEGORIES)),
                ),
            )
        ).all()
        if variant_ids
        else []
    )
    facts_by_variant: dict[int, list[dict]] = {}
    for f in fact_rows:
        facts_by_variant.setdefault(f.variant_id, []).append(
            {"category": f.category, "fact_key": f.fact_key, "value": f.fact_value,
             "unit": f.unit, "cycle": f.cycle, "source_id": f.source_id}
        )

    # 批量：系列/品牌（展示与官方页）
    series_map = {
        s.id: s
        for s in db.scalars(
            select(VehicleSeries).where(VehicleSeries.id.in_({v.series_id for v in variants}))
        ).all()
    }
    brand_map = {
        b.id: b
        for b in db.scalars(
            select(Brand).where(Brand.id.in_({s.brand_id for s in series_map.values()}))
        ).all()
    }

    # 维护便利性的统一数据代理（评审：全量数据已在库内，不再「暂无统一数据源」）：
    # = 品牌在库车系数（规模/网络） + 该品牌是否有月度销量记录（渠道活跃度）
    brand_series_count: dict[int, int] = {}
    for s in series_map.values():
        brand_series_count[s.brand_id] = brand_series_count.get(s.brand_id, 0) + 1
    brands_with_sales = set(
        db.scalars(
            select(VehicleSeries.brand_id).join(MonthlySales, MonthlySales.series_id == VehicleSeries.id).distinct()
        ).all()
    )

    scored: list[tuple[float, dict]] = []
    for variant in variants:
        series = series_map.get(variant.series_id)
        brand = brand_map.get(series.brand_id) if series else None
        fact_list = facts_by_variant.get(variant.id, [])
        facts = {(f["category"], f["fact_key"]): f for f in fact_list}

        # 第一层：硬约束
        price = prices.get(variant.id)
        if price is None:
            continue
        # 用户已点名的车系（会话锁定）：只从这些车系里选，直到用户要求看其他（评审：指定车型优先）
        if profile.locked_series_ids and variant.series_id not in profile.locked_series_ids:
            continue
        if profile.budget.min is not None and price < profile.budget.min:
            continue
        if profile.budget.max is not None and price > profile.budget.max:
            continue
        if profile.body_type and variant.body_type not in profile.body_type:
            continue
        if profile.energy_preference:
            prefs = profile.energy_preference
            if "new_energy" in prefs and variant.energy_type not in NEW_ENERGY_TYPES:
                continue
            if "fuel" in prefs and variant.energy_type in NEW_ENERGY_TYPES:
                continue
            if (
                variant.energy_type not in prefs
                and "new_energy" not in prefs
                and "fuel" not in prefs
            ):
                continue
        # 排除偏好（评审 L2）：用户明确「不要」的能源/车身直接硬过滤
        avoided = set(profile.avoid or [])
        if avoided:
            if variant.energy_type in avoided or (variant.body_type and variant.body_type in avoided):
                continue
            if "new_energy" in avoided and variant.energy_type in NEW_ENERGY_TYPES:
                continue
            if "fuel" in avoided and variant.energy_type not in NEW_ENERGY_TYPES:
                continue
        seats = _extract_seats(facts)
        if profile.passengers is not None and seats is not None and seats < profile.passengers:
            continue

        # 第二层：软评分
        dims: dict[str, float] = {}
        tradeoffs: list[str] = []

        # 预算匹配
        budget = profile.budget
        if budget.max and budget.min:
            span = max(budget.max - budget.min, 1)
            dims["budget"] = 1.0 - min(abs(price - (budget.min + budget.max) / 2) / span, 1.0)
        elif budget.max:
            dims["budget"] = max(0.0, 1.0 - max(price - budget.max, 0) / max(budget.max, 1))
        else:
            dims["budget"] = 0.5  # 无预算信息，中性

        # 用途匹配（通勤→轿车/纯电优先；家庭/长途→SUV/MPV）
        usage_hits = 0
        body = variant.body_type
        if "通勤" in profile.usage and body == "sedan":
            usage_hits += 1
        if any(u in profile.usage for u in ("家庭", "长途", "自驾")) and body in ("suv", "mpv"):
            usage_hits += 1
        if "长途" in profile.usage and variant.energy_type == "PHEV":
            usage_hits += 1
        dims["usage"] = min(usage_hits / max(len(profile.usage), 1), 1.0) if profile.usage else 0.5

        # 空间匹配（车长与座位数）
        length = _extract_length(facts)
        if length is None:
            dims["space"] = 0.5
        else:
            dims["space"] = min(max((length - 4300) / (5200 - 4300), 0.0), 1.0)

        # 能耗匹配
        if profile.energy_preference:
            dims["energy"] = 1.0
        elif variant.energy_type in NEW_ENERGY_TYPES:
            dims["energy"] = 0.7
        else:
            dims["energy"] = 0.4

        # 动力匹配
        power = _extract_power(facts)
        if power is None:
            dims["power"] = 0.5
        else:
            dims["power"] = min(max((power - 80) / (300 - 80), 0.0), 1.0)

        # 舒适性 / 智能化（存在对应类别事实即得分）
        dims["comfort"] = 1.0 if any(k[0] in _COMFORT_CATEGORIES for k in facts) else 0.0
        dims["intelligence"] = (
            1.0 if any(k[0] in _INTELLIGENCE_CATEGORIES for k in facts) else 0.0
        )

        # 维护便利性（统一数据源代理：品牌规模 + 销量活跃度）。
        # 注：不再输出「暂无数据源/未参与评分」等内部说明（评审：不应呈现给用户）
        brand_scale = min(brand_series_count.get(series.brand_id, 0) / 5.0, 1.0)
        channel_active = 1.0 if series.brand_id in brands_with_sales else 0.0
        dims["maintenance"] = min(1.0, 0.4 + 0.3 * brand_scale + 0.3 * channel_active)

        total_weight = sum(weights[d] for d in dims)
        score = sum(weights[d] * dims[d] for d in dims) / total_weight if total_weight else 0.0

        matched = []
        if dims["budget"] >= 0.8:
            matched.append("预算匹配")
        if profile.usage and dims["usage"] >= 0.5:
            matched.append("用途匹配")
        if profile.energy_preference and dims["energy"] == 1.0:
            matched.append("能源偏好匹配")
        if profile.passengers is not None and seats is not None and seats >= profile.passengers:
            matched.append(f"座位满足（≥{profile.passengers} 座）")

        scored.append(
            (
                score,
                {
                    "variant_id": variant.id,
                    "series_id": variant.series_id,
                    "series_name": series.name if series else None,
                    "brand_name": brand.name if brand else None,
                    "display_name": variant.display_name,
                    "energy_type": variant.energy_type,
                    "price_cny": price,
                    "score": round(score, 4),
                    "matched": matched,
                    "tradeoffs": tradeoffs,
                    "official_page_url": series.official_page_url if series else None,
                    "source_id": variant.source_id,
                },
            )
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[:limit]
    return {
        "count": len(scored),
        "variants": [item[1] for item in top],
        "weights_used": weights,
    }


# ── 工具 7：citation_verifier ─────────────────────────────────────────────────
def citation_verifier(claims: list[dict], sources: list[dict]) -> tuple[bool, list[str]]:
    """检查回答中的事实是否都有证据（source_id / source_name）。"""
    problems = []
    source_ids = {s.get("source_id") for s in sources if s.get("source_id") is not None}
    for claim in claims:
        if not claim.get("source_id"):
            problems.append(f"无来源：{claim.get('label', claim)}")
        elif claim["source_id"] not in source_ids and sources:
            problems.append(f"来源不在证据集内：{claim.get('label', claim)}")
    return (not problems, problems)


# ── 工具 8：safety_guard ──────────────────────────────────────────────────────
_FORBIDDEN_HINTS = ("优惠", "库存", "成交价", "落地价", "提车", "下单", "贷款", "购置税", "保险")
# 否定语境（注意：「免息/免购置税」是促销词；单字「无」会误放行「无条件免息贷款」，故不收录）
_NEGATION_HINTS = ("不含", "不包含", "不包括", "不涉及", "没有", "不提供", "无需")


def safety_guard(text: str) -> tuple[bool, str | None]:
    """拦截优惠、库存、交易与无来源事实。

    否定表述（如「本报价不包含购置税/保险」）不算违规（评审 L6），
    逐处检查命中词前方是否存在否定语境。
    """
    for hint in _FORBIDDEN_HINTS:
        start = 0
        while True:
            i = text.find(hint, start)
            if i < 0:
                break
            prefix = text[max(0, i - 10):i]
            if not any(neg in prefix for neg in _NEGATION_HINTS):
                return (False, f"回答包含被禁止的内容类别：{hint}")
            start = i + len(hint)
    return (True, None)


# ── LLM 工具 Schema（生产 Agent Worker 工具调用循环使用）───────────────────────
# 当前引擎为确定性主链 + LLM 仅解释；启用工具调用循环时，把 TOOL_SCHEMAS 传入
# LLMClient.chat(tools=...)，并按下表把模型请求映射到同名函数（见 tests/test_tools.py 校验）。
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "vehicle_search",
            "description": "查询在售车型系列（可带关键字、车身类型、能源类型、品牌过滤）。只返回数据库中的事实。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "body_type": {"type": "string", "enum": ["sedan", "suv", "mpv"]},
                    "energy_type": {"type": "string"},
                    "brand_id": {"type": "integer"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sales_search",
            "description": "查询指定月份的车型零售销量排行（默认最近完整自然月）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "month": {"type": "string", "description": "YYYY-MM"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vehicle_evidence",
            "description": "返回 SKU 的配置、价格与官方来源，作为事实依据。",
            "parameters": {
                "type": "object",
                "properties": {"variant_id": {"type": "integer"}},
                "required": ["variant_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieval_search",
            "description": "混合检索官方文档与结构化事实（BM25∥向量多路召回 + RRF 融合 + 重排），返回带来源的证据切片。top_k 范围 1~10。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "filters": {
                        "type": "object",
                        "description": "元数据过滤，允许键：brand_id/series_id/model_year_id/variant_id/source_id/energy_type",
                    },
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommendation_tool",
            "description": "按用户画像执行硬条件筛选与软评分，返回候选 SKU。",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget_min": {"type": "number"},
                    "budget_max": {"type": "number"},
                    "passengers": {"type": "integer"},
                    "body_type": {"type": "array", "items": {"type": "string"}},
                    "energy_preference": {"type": "array", "items": {"type": "string"}},
                    "usage": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 5},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "official_link_tool",
            "description": "返回车型系列的官方车型页链接。",
            "parameters": {
                "type": "object",
                "properties": {"series_id": {"type": "integer"}},
                "required": ["series_id"],
            },
        },
    },
]
