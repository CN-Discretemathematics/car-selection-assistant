"""品牌提及解析（把「只要奔驰」这类自然语言要求解析为库内品牌 id）。

为什么需要（2026-09 用户实测）：
用户第一句就说「必须是奔驰」，但当时的画像里**没有品牌字段**，于是即便收齐了
预算/用途/人数，推荐 SQL 也从不按品牌过滤，最终推了领克、小鹏、大众、林肯——
用户要的核心硬约束被整条链路丢掉了。

实现要点：
- 只匹配库内 `brands.name` 与 `brands.aliases`（不匹配车系名），长名优先，
  避免「一汽-大众」这类长名被「大众」抢先吃掉；
- 识别否定语境（不要/不考虑/除了…）→ 记为排除品牌，而不是正向约束；
- 对**易与日常词混淆**的品牌名（理想/长安/大众/未来）要求出现购车意图词，
  否则不当作品牌约束（「理想预算 20 万」不应解析成品牌「理想」）。
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.catalog.series_index import normalize_name
from app.common.models import Brand, VehicleSeries, VehicleVariant

# 否定语境前缀（作用于品牌名之前 4 个字以内）
NEGATION_PREFIXES = ("不要", "不考虑", "不想", "不想要", "不选", "别买", "别要", "除了", "排除", "拒绝")
# 购车意图词（宽松）：用于判断消息是否在谈买车
INTENT_WORDS = (
    "品牌", "买", "购", "选", "看", "要", "只", "必须", "车型", "车", "suv", "mpv", "轿车", "预算",
)
# 强意图词（用于消解「理想/长安/大众」这类日常词歧义）：
# 只认明确的购车动作，不含「预算/要/看/车」等过宽的词，否则「理想的预算 20 万」会误判成品牌「理想」
STRONG_INTENT_WORDS = ("品牌", "买", "购", "选车", "只要", "必须", "就要", "想要", "考虑", "关注", "看看")
# 易与日常词混淆的品牌名（归一化后）：仅在同句出现购买意图词时才作约束
AMBIGUOUS_BRANDS = {"理想", "长安", "大众", "未来", "启辰", "东风"}

# 品牌名索引缓存（指纹 = 活跃品牌数 + max(id)）；pytest 每次新建库，指纹可能碰撞，故测试下禁用
_cache: dict = {"fingerprint": None, "entries": ()}


def _load_entries(db: Session) -> tuple[tuple[str, int, str], ...]:
    """(归一化品牌名或别名, brand_id, 展示名)，长名优先。"""
    rows = db.execute(
        select(Brand.id, Brand.name, Brand.aliases).where(Brand.active_status == "active")
    ).all()
    entries: list[tuple[str, int, str]] = []
    for brand_id, name, aliases in rows:
        for candidate in (name, *(aliases or [])):
            normalized = normalize_name(candidate or "")
            if normalized:
                entries.append((normalized, brand_id, name))
    entries.sort(key=lambda item: -len(item[0]))
    return tuple(entries)


def brand_entries(db: Session) -> tuple[tuple[str, int, str], ...]:
    return _load_entries(db)


# 明确品牌约束的语气（「只要奔驰」「必须是奔驰」「想买奔驰」）
BRAND_INTENT_RE = re.compile(
    r"(只要|只考虑|只看|只买|必须是|必须|就要|想要|想买|打算买|要买|买个|购买|买|锁定)"
)


def _is_bare_brand(normalized_message: str, normalized_brand: str) -> bool:
    """消息是否基本只有品牌名（用户在回答「哪个品牌」这类追问，如只回「奔驰」）。"""
    remainder = normalized_message.replace(normalized_brand, "")
    return len(remainder) <= 2


def resolve_brand_mentions(
    db: Session,
    message: str,
    *,
    series_names: list[str] | None = None,
    assume_constraint: bool = False,
) -> dict:
    """解析消息中的品牌 → {"brand_ids": [...], "brand_labels": [...], "brand_exclude_ids": [...]}。

    两处消歧（都来自实测回归）：
    1. **品牌名出现在被点名车系的名字里时不算品牌约束**——「银河星愿怎么样」里的「银河」
       是对车系的指代；若当成「只要银河」，下一轮按 银河+燃油 过滤就会得到空结果。
    2. **只有明确约束语气**（只要/必须/想买…）、消息基本只有品牌名、或提问本身就是
       品牌盘点（assume_constraint，如「奔驰都有哪些车型」）时，才升级为硬约束；
       单纯提及（「比亚迪和吉利哪个好」）不写进画像。
    """
    normalized = normalize_name(message)
    if not normalized:
        return {}
    has_intent = any(word in message.lower() for word in INTENT_WORDS)
    has_strong_intent = any(word in message for word in STRONG_INTENT_WORDS)
    series_norms = [normalize_name(name) for name in (series_names or []) if name]
    explicit = assume_constraint or bool(BRAND_INTENT_RE.search(message))
    include: dict[int, str] = {}
    exclude: dict[int, str] = {}
    for name, brand_id, label in _load_entries(db):
        start = normalized.find(name)
        if start < 0:
            continue
        if any(name in series_norm for series_norm in series_norms):
            continue  # 品牌名属于被点名的车系名（「银河星愿」）→ 是对车系的指代
        if name in AMBIGUOUS_BRANDS and not (has_intent and has_strong_intent):
            continue  # 「理想的预算 20 万」不当作品牌「理想」；「我想买理想」才算
        if not (explicit or _is_bare_brand(normalized, name)):
            continue  # 只是提及，不构成硬约束
        prefix = normalized[max(0, start - 4): start]
        if any(neg in prefix for neg in NEGATION_PREFIXES):
            exclude[brand_id] = label
        else:
            include[brand_id] = label
    # 同一品牌既被正向提及又被否定时以否定为准（保守：宁可少推不可推错）
    for brand_id in list(include):
        if brand_id in exclude:
            include.pop(brand_id)
    result: dict = {}
    if include:
        result["brand_ids"] = sorted(include)
        result["brand_labels"] = [include[i] for i in sorted(include)]
    if exclude:
        result["brand_exclude_ids"] = sorted(exclude)
    return result


def catalog_overview(
    db: Session,
    *,
    body_types: list[str] | None = None,
    energy_allowed: set[str] | None = None,
) -> dict:
    """全库在售盘点（确定性，供「全部车型有多少款车」这类计数问题）。

    背景（2026-09-17 用户实测）：这句话此前既没进品牌盘点、也没进工具循环，
    直接落进推荐链去追问预算。数量类问题必须读库如实报数。

    口径与站内列表页一致：车系取 `VehicleSeries.active_status == "active"`，
    款型取 `VehicleVariant.status == "on_sale"`；能源分桶语义同 `/vehicles?energy_type=`。

    **能源分桶一律不做减法**（2026-09-17 评审 B3）：`energy_types` 为空的车系既不算燃油、
    也不算新能源，单列 `unlabeled_series_count`。真库实测 23 个在售车系里 20 个未标注，
    「总数 − 燃油」会答出「新能源 22 个」，而按站内口径实际只有 2 个——把缺失值当事实。

    可选过滤（把「SUV 有多少款车」「有多少款新能源车」这类问句答成子集计数，而不是全库数）：
    `body_types` 命中任一即计入；`energy_allowed` 是**已展开的具体能源类型集合**
    （如 BEV/PHEV/HEV/ICE）——泛化词 `new_energy`/`fuel` 由调用方按引擎口径
    （`_expand_energy_prefs`）展开后再传，避免这里再维护一套词表而与推荐链不一致。
    """
    from app.common.enums import NEW_ENERGY_TYPES

    new_energy_set = set(NEW_ENERGY_TYPES)
    rows = db.execute(
        select(
            VehicleSeries.id,
            VehicleSeries.brand_id,
            VehicleSeries.body_type,
            VehicleSeries.energy_types,
            VehicleSeries.source_id,
        )
        # 内连接 Brand：孤儿车系（无品牌/品牌缺失）不计入——口径与站内列表页
        # （vehicles/router.py 的 /vehicles）完全一致；列表页同样不按品牌 active 过滤，
        # 故这里也不过滤，两边数字永远可以对得上（2026-09-17 评审建议 3）。
        .join(Brand, Brand.id == VehicleSeries.brand_id)
        .where(VehicleSeries.active_status == "active")
    ).all()

    wanted_types = set(energy_allowed or ())
    wanted_body = set(body_types or [])

    def keep(row) -> bool:
        if wanted_body and row.body_type not in wanted_body:
            return False
        if not wanted_types:
            return True
        return bool(set(row.energy_types or []) & wanted_types)

    selected = [row for row in rows if keep(row)]
    # 款型数按车系分组一次查完（避免逐车系 N+1；也不用把 1k+ id 塞进 IN 列表）
    variants_per_series = dict(
        db.execute(
            select(VehicleVariant.series_id, func.count(VehicleVariant.id))
            .where(VehicleVariant.status == "on_sale")
            .group_by(VehicleVariant.series_id)
        ).all()
    )
    fuel_count = sum(1 for r in selected if (set(r.energy_types or []) - new_energy_set))
    nev_count = sum(1 for r in selected if (set(r.energy_types or []) & new_energy_set))
    unlabeled = sum(1 for r in selected if not r.energy_types)
    without_variants = sum(1 for r in selected if not variants_per_series.get(r.id))
    # 来源按覆盖车系数排序（同数时按 id 稳定排序，避免引用随扫描顺序抖动）
    source_counts: dict[int, int] = {}
    for row in selected:
        if row.source_id:
            source_counts[row.source_id] = source_counts.get(row.source_id, 0) + 1
    source_ids = [sid for sid, _ in sorted(source_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
    return {
        "series_count": len(selected),
        "variant_count": sum(variants_per_series.get(r.id, 0) for r in selected),
        "brand_count": len({r.brand_id for r in selected if r.brand_id}),
        "fuel_series_count": fuel_count,
        "new_energy_series_count": nev_count,
        "unlabeled_series_count": unlabeled,
        # 燃油与新能源可重叠（同一车系两种款型都有），故不保证 fuel + nev == series_count
        "energy_overlap_count": sum(
            1
            for r in selected
            if (set(r.energy_types or []) - new_energy_set) and (set(r.energy_types or []) & new_energy_set)
        ),
        "without_variants": without_variants,
        "source_ids": source_ids,
    }


def brand_series_overview(db: Session, brand_ids: list[int], budget_max: float | None = None) -> dict:
    """品牌车系概览（确定性，供「奔驰都有哪些车型」这类列举问题）。

    返回：车系总数、按能源类型分布（含燃油 / 仅新能源）、各价格区间车系（可选按预算上限筛），
    以及无价格数据的车系数——让回答能如实说明数据完整度，而不是靠模型记忆。
    """
    from app.catalog import services as catalog
    from app.common.enums import NEW_ENERGY_TYPES

    if not brand_ids:
        return {"brand_names": [], "series_count": 0, "fuel_series_count": 0,
                "new_energy_series_count": 0, "without_price": 0, "series": []}
    brand_names = {
        bid: name
        for bid, name in db.execute(select(Brand.id, Brand.name).where(Brand.id.in_(brand_ids))).all()
    }

    series_rows = db.scalars(
        select(VehicleSeries).where(
            VehicleSeries.brand_id.in_(brand_ids),
            VehicleSeries.active_status == "active",
        )
    ).all()
    items: list[dict] = []
    fuel_count = 0
    for series in series_rows:
        energy = list(series.energy_types or [])
        is_fuel = any(t not in NEW_ENERGY_TYPES for t in energy)
        price_min, price_max = catalog.series_price_range(db, series.id)
        if is_fuel:
            fuel_count += 1
        items.append(
            {
                "series_id": series.id,
                "series_name": series.name,
                "brand_name": brand_names.get(series.brand_id),
                "energy_types": energy,
                "has_fuel": is_fuel,
                "price_min": float(price_min) if price_min is not None else None,
                "price_max": float(price_max) if price_max is not None else None,
                "source_id": series.source_id,
            }
        )
    if budget_max is not None:
        items = [i for i in items if i["price_min"] is not None and i["price_min"] <= budget_max]
    items.sort(key=lambda i: (i["price_min"] is None, i["price_min"] or 0))
    return {
        "brand_names": [brand_names[i] for i in sorted(brand_names)],
        "series_count": len(items),
        "fuel_series_count": fuel_count,
        "new_energy_series_count": len(items) - fuel_count,
        "without_price": sum(1 for i in items if i["price_min"] is None),
        "series": items,
    }
