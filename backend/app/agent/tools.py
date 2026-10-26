"""Agent 业务工具。

每个能力都是类型化工具，全部在 Agent Worker 内实现，只经过 catalog 服务层访问数据；
不直接访问 SQL、Milvus、对象存储或任意网络资源。LLM 只能调用这些工具，
工具的返回值是回答中车辆事实的唯一来源。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.agent import scoring_v2
from app.agent.schemas import UserProfile
from app.agent.weights import (
    DEFAULT_WEIGHTS,
    DIM_LABELS,
    DIM_LABELS_V2,
    TRADEOFF_GAP,
    V2_WEIGHT_KEY_ALIASES,
    WEIGHT_CEILING,
)
from app.catalog import services as catalog
from app.common.enums import NEW_ENERGY_TYPES, expand_avoid, expand_energy_prefs
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
    brand: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """按条件查询在售车型系列与款型。

    brand 支持**品牌名**（如「奔驰」）：模型拿不到库内 brand_id，此前只能凭记忆列举品牌车型，
    实测会把「奔驰在售 57 款、燃油 37 款」答成「3 款、都是纯电」。品牌名走 brands 表
    （名称 + 别名）精确解析，解析不到时返回空列表，由调用方如实说明。
    """
    stmt = select(VehicleSeries).where(VehicleSeries.active_status == "active")
    if query:
        # 转义 LIKE 通配符：query 来自模型/用户，`%`/`_` 会被当作通配符放大匹配面
        safe_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(VehicleSeries.name.like(f"%{safe_query}%", escape="\\"))
    if body_type:
        stmt = stmt.where(VehicleSeries.body_type == body_type)
    if brand_id:
        stmt = stmt.where(VehicleSeries.brand_id == brand_id)
    if brand:
        from app.catalog.brands import resolve_brand_mentions

        resolved = resolve_brand_mentions(db, brand)
        ids = resolved.get("brand_ids") or []
        if not ids:
            return []  # 品牌不在库内：如实返回空，不猜
        stmt = stmt.where(VehicleSeries.brand_id.in_(ids))
    # limit 夹紧 1..200：模型给的 limit 可能是 10^9（放大成 N+1 查询）或负数
    limit = max(1, min(int(limit), 200))
    series_list = db.scalars(stmt.limit(limit)).all()

    result = []
    for series in series_list:
        # 不复用参数名：brand 是「品牌名」字符串，已在上面的
        # `if brand:` 里消费完；在这里重引用为 Brand 对象会把参数的类型从
        # str | None 变成 Brand | None。当前行为正确（字符串分支在循环外），但
        # 是一个型别陷阱：若以后有人把 `if brand:` 移进循环内就会静默失效。
        brand_row = db.get(Brand, series.brand_id)
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
                "brand_name": brand_row.name if brand_row else None,
                "body_type": series.body_type,
                "energy_types": energy_types,
                "price_range": {
                    "min": float(price_min) if price_min is not None else None,
                    "max": float(price_max) if price_max is not None else None,
                },
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
    # limit 夹紧（模型可传 0/负数/超大值；2026-09-14 第三轮审查 L4）
    # 注意：docstring 必须是函数首语句（用户文案门禁靠 AST 识别 docstring）
    limit = max(1, min(int(limit), 50))
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
        return {"variant_id": variant_id, "error": "未找到该款型"}
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
        "source_id": variant.source_id,
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


def _profile_has_no_soft_info(profile: UserProfile) -> bool:
    """用户**说不清需求**吗（用途/人数/车身/能源一个都没说）。

    2026-10-23（生产真实用户会话实测）：用户问「15万预算有什么能选的」，
    画像里**只有预算** ⇒ 软评分的 8 个维度几乎全是中性分（0.5）
    ⇒ 排序实际由「车长/功率」这类客观参数决定 ⇒ 给用户的不是「热销的」，
    而是「参数最唬人的」——对「不知道要什么」的用户**毫无参考价值**。

    ⇒ 这时**按销量排**（用户建议：推荐销量高的车型）。
    候选**集合不变**（硬约束照旧过滤），只改顺序 ⇒ 风险极低。
    """
    return not (
        profile.usage
        or profile.passengers is not None
        or profile.body_type
        or profile.energy_preference
    )


def _latest_sales_by_series(db: Session, series_ids: set[int]) -> tuple[str, dict[int, int]]:
    """车系 → 最新月的销量（口径与 `sales_search` 一致：**零售优先、门户回退**）。

    返回 `(月份, {车系 id: 销量})`；没有销量数据时返回 `(月份, {})`。
    """
    if not series_ids:
        return "", {}
    month = catalog.latest_sales_month(db)
    if not month:
        return "", {}
    rows = db.execute(
        select(MonthlySales.series_id, MonthlySales.sales_type, MonthlySales.sales_count)
        .where(
            MonthlySales.month == month,
            MonthlySales.series_id.in_(series_ids),
            MonthlySales.sales_type.in_(("retail", "portal")),
        )
        .order_by(MonthlySales.sales_count.desc())
    ).all()
    out: dict[int, int] = {}
    for sid, stype, count in rows:
        # 与 `sales_search` 同一口径：零售优先（后到的 retail 覆盖 portal），
        # 但**后到的 portal 不覆盖 retail**。
        if sid not in out or stype == "retail":
            out[int(sid)] = int(count or 0)
    return month, out


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
# ── 配置类事实的类别归属（2026-10-24 生产实测后修正）────────────────────────
#
# ⚠️ 此前 `_COMFORT_CATEGORIES` = ("舒适性", "内部配置", "外部配置")，
# **漏掉了「安全配置」**——而它是生产库里**第二大**的配置类事实
# （10.5 万条，其中 8.7 万条是「●」标配），量级与「内部配置」（11.8 万）相当。
#
# ⇒ 漏掉它的直接后果：**安全配置差异完全不影响打分**。
# 生产实测（逸动三个款型，同车系）：
#   劲享型 8.79万：安全配置 ● 8 项
#   悦享型 9.39万：安全配置 ● 10 项
#   智享型 9.99万：安全配置 ● 13 项   ← 配置明显更多，却**分数完全相同**
#
# 「舒适性」这个键在生产数据里**根本不存在**（真实类别是「内部配置/外部配置/
# 安全配置/智能辅助驾驶/操控配置/个性化」）⇒ 保留它只是为了兼容旧数据。
_COMFORT_CATEGORIES = ("舒适性", "内部配置", "外部配置", "安全配置", "操控配置")
_INTELLIGENCE_CATEGORIES = ("智能驾驶", "智能座舱", "智能/辅助驾驶")

# ── 评分 v2 事实查询范围（AGENT_SCORING_MODE=new 时才用）────────────────────
# 只取七维真正会读的键与类目，避免把全库 74 万条事实都拉进内存。
_V2_PARAM_FACT_KEYS = (
    "轴距(mm)",
    "轴距",
    "长*宽*高(mm)",
    "长度(mm)",
    "车长(mm)",
    "宽度(mm)",
    "后备厢容积(L)",
    "后备厢容积",
    "行李厢容积(L)",
    "整备质量(kg)",
    "整备质量",
    "座位数(个)",
    "座位数",
    "座位数（个）",
    "seats",
)
# 油耗/电耗/续航/功率在库内的 fact_key 带工况后缀（WLTC/NEDC/CLTC/最低荷电状态），
# 无法穷举 ⇒ 用 LIKE 圈定后再交给 scoring_v2 的白名单/工况优先级解析。
_V2_FACT_KEY_PATTERNS = ("%油耗%", "%耗电量%", "%续航%", "%功率%", "%马力%")
_V2_CONFIG_CATEGORIES = (
    scoring_v2.SAFETY_CATEGORY,
    *scoring_v2.COMFORT_CATEGORIES_V2,
    *scoring_v2.INTELLIGENCE_CATEGORIES_V2,
)

#: 配置表里表示「标配」的**标记字符**（汽车之家配置表的约定）。
#:
#: 2026-10-24 生产实测的取值分布（配置类事实共约 39 万条）：
#:   '●' 34.3 万 · '前● / 后●' 2.1 万 · '主● / 副●' 1.1 万 · '前● / 后-' 0.7 万 …
#:   '○' 1.2 万（选装）· '-' / '—'（没有）
#:
#: ⇒ 标配标记是**字符 '●'**，且常**嵌在复合取值里**（'前● / 后●'）⇒
#:   不能用「值 == '●'」这种等值判断（那会漏掉 4 万多条标配项）。
#:
#: 判定：**值里含 '●' 即算有该标配**；含 '○'（选装）**不算**；
#: 复合取值里 '●' 与 '-'/'○' 混排时（如 '前● / 后-'）**算有**——
#: 至少有一部分是标配，比完全没有强。
#:
#: ⚠️ 选装（'○'）**不计入**：把选装算成标配等于谎称这台配置更好。
_STANDARD_MARK = "●"


def _count_standard_items(fact_list: list[dict], categories: tuple[str, ...]) -> int:
    """该款型在给定类别下的**标配项数**。

    判定「含标配标记 '●'」——见 `_STANDARD_MARK` 的说明（取值有多种形态，
    含复合形态 '前● / 后●'，不能用等值判断）。

    ⚠️ 这是**相对**指标：不同车系之间不可比（紧凑级天然配置项少），
    所以只在**同批次内**做归一化，不做跨车系比较。
    """
    n = 0
    for f in fact_list:
        if f.get("category") not in categories:
            continue
        value = str(f.get("value") or "")
        if _STANDARD_MARK in value:
            n += 1
    return n

# 取舍叙事的维度名（与 matched 的中文风格一致，面向用户）
#
# 2026-10-20：定义已搬到 `app/agent/weights.py`（与 DEFAULT_WEIGHTS / WEIGHT_CEILING
# 同属一份知识）。此处保留**私有别名**以免改动本文件内所有 `_DIM_LABELS` 引用，
# 并对外 re-export 公开名（`DEFAULT_WEIGHTS` 等）——外部模块历史上从这里 import，
# 改路径会打断它们。
_DIM_LABELS = DIM_LABELS
_TRADEOFF_GAP = TRADEOFF_GAP

__all__ = [
    "DEFAULT_WEIGHTS",
    "DIM_LABELS",
    "TRADEOFF_GAP",
    "WEIGHT_CEILING",
    "recommendation_tool",
]


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


def _tradeoff_gaps(
    dims: dict[str, float],
    measured: set[str],
    best_by_dim: dict[str, float],
    labels: dict[str, str] | None = None,
) -> list[str]:
    """该候选相对**本批最优**让出了什么（纯计算，不调 LLM、不引入任何外部事实）。

    只在「本候选有库内实值」且「落后达阈值」时报出：
    - 未列入 measured 的维度是**库内无此数据**，说成「不及最优」等于编造负面事实；
    - 落后不足 _TRADEOFF_GAP 的是噪声，报出来只会稀释真正的取舍。
    """
    gaps: list[str] = []
    for dim, label in (labels or _DIM_LABELS).items():
        if dim not in measured or dim not in best_by_dim:
            continue
        if best_by_dim[dim] - dims[dim] >= _TRADEOFF_GAP:
            gaps.append(f"{label}不及本批最优候选")
    return gaps


# 候选集超过此数即告警：说明本次推荐退化成「全库扫描 + 全量评分」。
# 单纯加 SQL LIMIT 会改变推荐结果，故先量化再决策（见 recommendation_tool 内注释）。
_CANDIDATE_SCAN_WARN = 500


# 8 维软评分默认权重（§17.2）与单维权重上限：
#
# 2026-10-20：定义已搬到 `app/agent/weights.py`，与 `WEIGHT_RAISE`（强调增量）、
# `WEIGHT_DIM_KEYWORDS`（维度关键词）、`USAGE_HINTS`（用途词表）同属一份知识。
#
# 此前它们**散在三个 module**（tools / engine），导致 `engine` 顶层 import
# `soft_prefs`、而 `soft_prefs` 又只能函数内反向 import `engine`——
# **真实的循环依赖**，那 4 处延迟 import 是它的疤痕。
# 团队当时已写下「两处各写一份必然漂移」（soft_prefs.py:723），
# 却选择用延迟 import 打补丁而不是收拢 module：补丁挡住了崩溃，没挡住漂移。
#
# 语义不变：`profile.weights` 里的值是**绝对权重**（覆盖），不是增量——
# API 直传权重（POST /recommendations {"weights": {...}}）依赖这一点，
# 故不在 tools 侧做叠加。


def _hard_constraint_stmt(profile: UserProfile):
    """v1/v2 共用的硬约束 SQL（候选集必须逐位等价）。

    v2 只替换“怎么打分”，不改变“哪些款型能进候选集”。两版共用这一份 SQL，
    并用候选集等价测试钉住；避免两套硬约束各改一半造成静默不一致。

    约束顺序与来源：
    - 在售 / 会话锁定车系 / 品牌白名单 / 品牌排除 / 车身 / 能源偏好与排除：
      全部下推 SQL。能源泛化（new_energy/fuel）的唯一实现在 app/common/enums.py，
      不在这里复制规则。
    - 当前生效指导价（official_msrp + effective_to IS NULL）+ 预算区间；
    - 用户明确拒绝过的款型（会话内，拒绝后不再推）；
    - 座位数：⚠️ `IS NULL` 这一支不能省。缺座位数据的款型必须保留（缺数据 ≠
      不满足）；写成 `seat_count >= N` 会在一次查询里丢掉 85% 未回填候选，
      且没有任何报错。实测本地快照 6629 款型里只有 1007 个有座位事实。
    """
    stmt = select(VehicleVariant).where(VehicleVariant.status == "on_sale")
    # 会话锁定的车系（用户点名过、尚未解锁）
    if profile.locked_series_ids:
        stmt = stmt.where(VehicleVariant.series_id.in_(profile.locked_series_ids))
    if profile.brand_ids:
        stmt = stmt.where(
            VehicleVariant.series_id.in_(
                select(VehicleSeries.id).where(VehicleSeries.brand_id.in_(profile.brand_ids))
            )
        )
    if profile.brand_exclude_ids:
        stmt = stmt.where(
            ~VehicleVariant.series_id.in_(
                select(VehicleSeries.id).where(VehicleSeries.brand_id.in_(profile.brand_exclude_ids))
            )
        )
    if profile.body_type:
        stmt = stmt.where(VehicleVariant.body_type.in_(profile.body_type))
    allowed = expand_energy_prefs(profile.energy_preference)
    if allowed:
        stmt = stmt.where(VehicleVariant.energy_type.in_(sorted(allowed)))
    energy_avoid, body_avoid = expand_avoid(profile.avoid)
    if energy_avoid:
        stmt = stmt.where(~VehicleVariant.energy_type.in_(sorted(energy_avoid)))
    if body_avoid:
        stmt = stmt.where(~VehicleVariant.body_type.in_(sorted(body_avoid)))
    price_cond = [
        OfficialPrice.effective_to.is_(None),
        OfficialPrice.price_type == "official_msrp",
    ]
    if profile.budget.min is not None:
        price_cond.append(OfficialPrice.price_cny >= profile.budget.min)
    if profile.budget.max is not None:
        price_cond.append(OfficialPrice.price_cny <= profile.budget.max)
    stmt = stmt.where(VehicleVariant.id.in_(select(OfficialPrice.variant_id).where(*price_cond)))
    if profile.rejected_variant_ids:
        stmt = stmt.where(~VehicleVariant.id.in_(profile.rejected_variant_ids))
    # 缺座位数的款型保留（缺数据 ≠ 不满足）。
    if profile.passengers is not None:
        stmt = stmt.where(
            or_(
                VehicleVariant.seat_count.is_(None),
                VehicleVariant.seat_count >= profile.passengers,
            )
        )
    return stmt


def _recommendation_v1(
    db: Session,
    profile: UserProfile,
    limit: int = 5,
    *,
    include_dims: bool = False,
    exclude_series_ids: Sequence[int] = (),
) -> dict:
    """确定性推荐（v1 八维内核）：PostgreSQL 硬条件筛选 + 软评分。

    事实全部来自数据库；评分维度无数据时不计分，禁止猜测。
    硬约束（在售/锁定车系/车身/能源/价格区间）全部下推 SQL（评审 P1：
    此前每次消息全量拉全部在售款型再 Python 过滤，大库下内存/延迟线性增长），
    剩余约束（座位数等基于事实的）在 Python 内完成。

    include_dims（2026-10-03，仅评测用，默认 **False**）：为真时把每个候选的
    逐维得分 `_dims` 与「本候选有实值的维度」`_measured` 一并返回，供
    `tools/eval_tradeoffs.py` 独立复核取舍叙事。**默认路径行为逐位不变**——
    这两个键照旧在返回前剥离，不进生产响应体。

    exclude_series_ids（2026-10-21，默认空）：**要排除的车系 id**——
    用于「再给一批」时排掉本会话**已经展示过**的车系。
    引擎是确定性的 ⇒ 不排除的话同一画像必然推回逐位相同的那一批
    （实测：宽需求下候选池 3162 台，第二轮仍是同一批 5 台）。
    ⚠️ 按**车系**排除而不是按款型：用户心里的「另一台车」是另一个**车系**，
    不是同款车的另一个配置（实测：第一轮推的 5 张卡常是同一车系的 5 个款型）。
    ⚠️ 与「被用户拒绝的款型」是**两件事**：拒绝是**永久**排除
    （`profile.rejected_variant_ids`，会话内一直生效），推过了只是这轮不想重复。
    """
    weights = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (profile.weights or {}).items() if v is not None}}

    # ── 第一层：硬约束下推 SQL ────────────────────────────────────────────
    # v1/v2 共用同一份实现（2026-10-27）：候选集只由硬约束决定，评分内核不能改变
    # “哪些款型能进候选集”。各条约束的取舍理由见 _hard_constraint_stmt 的注释。
    stmt = _hard_constraint_stmt(profile)

    # ── 候选集规模（2026-10-02 P5.1）────────────────────────────────────
    # 这条 stmt **故意不加 LIMIT**：排名由下方 8 维软评分决定，而评分需要全量事实，
    # SQL 侧无法复现同一排序；随手加 limit 会让「取前 N 条」变成「随便取 N 条」——
    # 推荐结果会静默改变，且没有任何东西能证明被丢掉的那些更差。
    #
    # 但代价是真实的：用户没给预算/品牌/锁定时，本 stmt 匹配**全库**在售款型，
    # 全部物化进 Python 再逐个评分（README 记录的 181s 端到端预算主要成分）。
    #
    # 先把量测出来再决定怎么改：一次 COUNT(*) 比物化全表便宜得多，
    # 记进日志与返回体的 candidates_scanned，让「多大」成为可观测事实而非猜测。
    #
    # ⚠️ 这个计数在**排除「已展示」之前**取：它是「符合当前条件的池子有多大」，
    # 执行层要靠它区分两种空结果——「条件本身没匹配到车」与「匹配到的都推过了」
    # （文案不同，不能混）。排除后的实际评分数见返回体的 `count`。
    candidates_scanned = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    if candidates_scanned > _CANDIDATE_SCAN_WARN:
        logging.getLogger("app.agent.recommendation").info(
            json.dumps(
                {
                    "candidates_scanned": candidates_scanned,
                    "limit": limit,
                    "note": "无预算/品牌/锁定约束时退化为全库扫描；加 LIMIT 会改变推荐结果",
                },
                ensure_ascii=False,
            )
        )

    # 「再给一批」：排掉本会话**已经展示过**的车系（2026-10-21）。
    # 放在计数**之后**、物化**之前**：计数要反映「池子有多大」（见上面的说明）。
    if exclude_series_ids:
        stmt = stmt.where(~VehicleVariant.series_id.in_(list(exclude_series_ids)))

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

    # 批量：系列/品牌（用于展示名与对比行的品牌信息）
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
    #
    # 2026-10-03 口径修正：原先 `brand_series_count` 是在**过滤后的候选集**上累加
    # （`for s in series_map.values()`，而 series_map 只含通过硬约束的车系）。于是
    # 「品牌规模」这个**品牌的固有属性**会随用户筛选条件变化——筛选得越窄，维护
    # 便利性看起来越好；一个品牌只剩 1 个车系通过筛选时 min(1/5,1)=0.2，规模被系统性
    # 低估。这不是命名问题，是维度算错了对象。
    # 改为**全库口径**（一次 GROUP BY 聚合，代价可忽略），并由
    # tests/test_brand_scale_full_catalog.py 钉住「与筛选条件无关」。
    brand_series_count: dict[int, int] = {
        int(bid): int(n)
        for bid, n in db.execute(
            select(VehicleSeries.brand_id, func.count()).group_by(VehicleSeries.brand_id)
        ).all()
    }
    brands_with_sales = set(
        db.scalars(
            select(VehicleSeries.brand_id).join(MonthlySales, MonthlySales.series_id == VehicleSeries.id).distinct()
        ).all()
    )

    scored: list[tuple[float, dict]] = []
    for variant in variants:
        series = series_map.get(variant.series_id)
        if series is None:
            # 悬空 series_id（导入期脏数据 / 车系已下线但款型残留）：跳过该款型。
            # 若不跳过，下方「维护便利性」维度会裸取 series.brand_id，
            # AttributeError 会让整个推荐接口 500——一处脏数据拖垮全链路。
            continue
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
            # 与上面的 SQL 下推同口径：泛化规则唯一实现在 enums（2026-10-02 收敛）。
            # 此前这里是**第三份**实现，且写法相反（逐 variant 判定而非集合展开），
            # 三处一旦漂移就会出现「SQL 已过滤、Python 侧又放行/拦掉」的不一致。
            allowed = expand_energy_prefs(profile.energy_preference)
            if allowed and variant.energy_type not in allowed:
                continue
        # 排除偏好（评审 L2）：用户明确「不要」的能源/车身直接硬过滤
        if profile.avoid:
            energy_avoid, body_avoid = expand_avoid(profile.avoid)
            if variant.energy_type in energy_avoid:
                continue
            if variant.body_type and variant.body_type in body_avoid:
                continue
        # 被用户明确拒绝过的款型：SQL 侧已下推，这里是**同口径的兜底**
        # （本仓惯例：下推 + Python 兜底，两处口径必须一致）
        if profile.rejected_variant_ids and variant.id in profile.rejected_variant_ids:
            continue
        # 已经展示过的**车系**（2026-10-21）：SQL 侧已排除，这里是**同口径的兜底**
        # （本仓惯例：下推 + Python 兜底，两处口径必须一致）
        # ⚠️ 与上面那条是**两件事**：拒绝是永久排除，推过了只是这轮不想重复。
        if exclude_series_ids and variant.series_id in exclude_series_ids:
            continue
        seats = _extract_seats(facts)
        # ⚠️ 这一行**必须保留**：它是下推的兜底，不是冗余。
        # `seat_count` 为 NULL 有两种情况——真的没座位事实，或**回填还没跑**。
        # 后者下推等于没生效，全靠这里兜住；两者语义一致（缺数据 ≠ 不满足）。
        if profile.passengers is not None and seats is not None and seats < profile.passengers:
            continue

        # 第二层：软评分
        dims: dict[str, float] = {}
        # measured：这些维度的分数来自**库内真实数据**，可以拿来跟别的候选比。
        # 未列入的维度（0.5 中性 / 0.0）代表**库内没有这项数据**，不是「这台更差」——
        # 把缺数据当成劣势去和别的车比较，正是本项目明令禁止的编造（设计原则第 1 条）。
        measured: set[str] = set()

        # 预算匹配（价格是库内实值；0.5 仅在用户没给预算时出现，仍可比较）
        budget = profile.budget
        if budget.max and budget.min:
            span = max(budget.max - budget.min, 1)
            dims["budget"] = 1.0 - min(abs(price - (budget.min + budget.max) / 2) / span, 1.0)
        elif budget.max:
            dims["budget"] = max(0.0, 1.0 - max(price - budget.max, 0) / max(budget.max, 1))
        else:
            dims["budget"] = 0.5  # 无预算信息，中性
        measured.add("budget")

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
        if profile.usage:
            measured.add("usage")

        # 空间匹配（车长与座位数）
        length = _extract_length(facts)
        if length is None:
            dims["space"] = 0.5          # 库内无车长 → 不是「空间小」
        else:
            dims["space"] = min(max((length - 4300) / (5200 - 4300), 0.0), 1.0)
            measured.add("space")

        # 能耗匹配（能源类型是库内实值）
        if profile.energy_preference:
            dims["energy"] = 1.0
        elif variant.energy_type in NEW_ENERGY_TYPES:
            dims["energy"] = 0.7
        else:
            dims["energy"] = 0.4
        measured.add("energy")

        # 动力匹配
        power = _extract_power(facts)
        if power is None:
            dims["power"] = 0.5          # 库内无功率 → 不是「动力弱」
        else:
            dims["power"] = min(max((power - 80) / (300 - 80), 0.0), 1.0)
            measured.add("power")

        # 舒适性 / 智能化（**标配项数**，不是「有/无」）
        #
        # 2026-10-24 生产实测缺陷：此前是**布尔**（有该类事实即 1.0）⇒
        # 同一车系的不同款型（配置不同）得分**逐位相同**：
        #   逸动 劲享型 8.79万 / 悦享型 9.39万 / 智享型 9.99万
        #   三款 score 全是 0.8816，8 个维度逐位一致 ⇒ 用户看到三张一样的分。
        # 根因：功率/车长/能源是**车系级**的，comfort/intelligence 是**布尔**的
        # ⇒ 同车系款型之间**没有任何一个维度能区分它们**。
        #
        # ⇒ 改为按**标配（●）项数**计分。绝对数跨车系不可比（紧凑级天然配置少），
        # 所以先记**原始项数**，再在**同批次内**归一化（见下方 second pass）：
        # 同一批里最多的那个 = 1.0，其余按比例。跨车系之间不因此可比——
        # 那由「本批最优」口径的取舍叙事负责表达，不混进这个维度。
        comfort_items = _count_standard_items(fact_list, _COMFORT_CATEGORIES)
        intelligence_items = _count_standard_items(fact_list, _INTELLIGENCE_CATEGORIES)
        dims["comfort"] = 0.0  # 占位，下面 second pass 归一化
        dims["intelligence"] = 0.0  # 占位，下面 second pass 归一化
        if comfort_items > 0:
            measured.add("comfort")
        if intelligence_items > 0:
            measured.add("intelligence")

        # 维护便利性（统一数据源代理：品牌规模 + 销量活跃度）。
        # 注：不再输出「暂无数据源/未参与评分」等内部说明（评审：不应呈现给用户）
        brand_scale = min(brand_series_count.get(series.brand_id, 0) / 5.0, 1.0)
        channel_active = 1.0 if series.brand_id in brands_with_sales else 0.0
        dims["maintenance"] = min(1.0, 0.4 + 0.3 * brand_scale + 0.3 * channel_active)
        measured.add("maintenance")

        total_weight = sum(weights[d] for d in dims)
        score = sum(weights[d] * dims[d] for d in dims) / total_weight if total_weight else 0.0

        matched = []
        if dims["budget"] >= 0.8:
            matched.append("预算匹配")
        if profile.usage and dims["usage"] >= 0.5:
            matched.append("用途匹配")
        if profile.energy_preference and dims["energy"] == 1.0:
            matched.append("能源偏好匹配")
        # 座位这条是**唯一可能被静默跳过**的硬约束：`seats is None`（库里查不到
        # 座位数）时既不满足也不筛掉——按「缺数据 ≠ 不满足」保留候选是对的，但
        # **对用户不披露就是假装校验过**。生产实测：用户明说「5 人以上」，
        # 70.2% 的在售款型查不到座位数，它们照样进列表且只字不提这条没校验。
        # 所以把「有没有真的校验过」作为数据带出（见 seat_verified / seat_check）。
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
                    # 用户点名了乘坐人数、但这台查不到座位数 → 座位这条**没校验**。
                    # 用户没提人数时本就没有座位约束可言，故为 True（无需披露）。
                    "seat_verified": profile.passengers is None or seats is not None,
                    # 占位：取舍必须等全部候选打完分才能算（要跟本批最优比），
                    # 由下方第二遍统一填。循环内无法计算。
                    "tradeoffs": [],
                    "source_id": variant.source_id,
                    # 私有：第二遍算取舍用，算完剥离
                    "_dims": dims,
                    "_measured": measured,
                    # 私有：comfort/intelligence 的**原始标配项数**（第二遍归一化用）
                    "_comfort_items": comfort_items,
                    "_intelligence_items": intelligence_items,
                },
            )
        )

    # ── comfort / intelligence 归一化（2026-10-24）──────────────────────────
    #
    # 循环内只能拿到**原始标配项数**，不能直接当分数用：绝对数跨车系不可比
    # （紧凑级天然配置项少）。⇒ 在**本批候选内**归一化：本批最多的 = 1.0，
    # 其余按比例；本批全是 0 ⇒ 该维度对所有候选都是 0（不是「缺数据」）。
    #
    # ⚠️ 归一化后**必须重算 score**——score 在循环内已算过一次（那时这两个维度
    # 是占位 0.0）。不重算 ⇒ 新维度对分数毫无影响 ⇒ 修复无效。
    _max_comfort = max((int(item[1].get("_comfort_items") or 0) for item in scored), default=0)
    _max_intelligence = max((int(item[1].get("_intelligence_items") or 0) for item in scored), default=0)
    if scored:
        for idx, (_score, item) in enumerate(scored):
            dims = item["_dims"]
            if _max_comfort > 0:
                dims["comfort"] = int(item.get("_comfort_items") or 0) / _max_comfort
            if _max_intelligence > 0:
                dims["intelligence"] = int(item.get("_intelligence_items") or 0) / _max_intelligence
            # 重算总分：权重表里各维度的权重不变，只是两维的取值变了
            total_weight = sum(weights[d] for d in dims)
            new_score = round(
                sum(weights[d] * dims[d] for d in dims) / total_weight if total_weight else 0.0, 4
            )
            item["score"] = new_score
            # ⚠️ **必须同步 tuple 里的分数**——排序用的是 `item[0]`（tuple 第一项），
            #    只改 dict 里的 score ⇒ 排序仍按旧分数 ⇒ 修复无效（且会让人以为改了）。
            scored[idx] = (new_score, item)

    # 排序口径（2026-10-23）：用户**说不清需求**时按**销量**排。
    #
    # 候选**集合不变**（硬约束照旧过滤），只改顺序 ⇒ 画像里有任何软线索时，
    # 走的还是原来的软评分排序 ⇒ 对既有推荐结果零影响。
    sales_by_series: dict[int, int] = {}
    if scored and _profile_has_no_soft_info(profile):
        _month, sales_by_series = _latest_sales_by_series(
            db, {int(item[1]["series_id"]) for item in scored}
        )
        for _score, item in scored:
            count = sales_by_series.get(int(item["series_id"]))
            if count is not None:
                # 带上销量数字：解释模块可以据此说「上个月卖了多少」，
                # 且这个数字**可溯源**（候选数据里的真实字段，不是模型编的）。
                item["sales_count"] = count
    if sales_by_series:
        scored.sort(
            key=lambda item: (int(item[1].get("sales_count") or 0), item[0]), reverse=True
        )
    else:
        scored.sort(key=lambda item: item[0], reverse=True)

    # ── 取舍叙事（2026-10-02 修活 L3，纯计算）────────────────────────────────
    # 「好处」是 matched（这台自身达标项），「代价」必须**相对同批候选**才有意义：
    # 在某个维度上低于本批最优，才算真的让出了什么。
    #
    # 两条硬约束（都是防编造，不是风格问题）：
    # 1. 只比较本候选**有真实数据**的维度（_measured）。space/power 的 0.5、
    #    comfort/intelligence 的 0.0 都表示「库内没这项数据」，拿它去说
    #    「这台不如别的车」就是**用缺失数据编造负面事实**，违反设计原则第 1 条。
    # 2. 差距小于 _TRADEOFF_GAP 一律不说——0.01 的落后不是取舍，是噪声。
    #
    # 文案不含数字：回答契约会校验正文数字必须可溯源，引入分数会把契约搞复杂。
    #
    # ⚠️ 对照集只取**用户明确提过的维度**（2026-10-05 用户实测反馈修正）。
    # 此前是 `if d in weights`，而 `weights` = `{**DEFAULT_WEIGHTS, **profile.weights}`
    # 含**全部 8 个默认维度**——于是无论用户说什么，取舍叙事都会拿全维度对照：
    # 用户说「我最看重动力」，推荐里照样出现「注意妥协项：空间不及本批最优候选」。
    #
    # 那不是编造（空间数据是真的、差距也是真的），但是**不相关的噪音**：
    # 把用户没提的维度摆出来，等于替用户决定他该在意什么。实测复现：
    # `weights={'power': 0.5}` 的画像，eπ007 报出
    # `['空间不及本批最优候选', '维护便利性不及本批最优候选']`——两个他都没提。
    #
    # `profile.weights` 的唯一来源是 `merge_profile` 从 `extract_hints` 累积的
    # 用户原话（「最看重动力」等），所以它就是「用户明确提过」的可靠来源。
    # 一个维度都没提 → 整段取舍不出现（而不是列举全部 8 维的差距）。
    user_dims = {d for d in (profile.weights or {}) if d in weights}
    best_by_dim: dict[str, float] = {}
    if user_dims:
        for _score, item in scored:
            for d in item["_measured"]:
                if d in user_dims:
                    best_by_dim[d] = max(best_by_dim.get(d, 0.0), item["_dims"][d])

    for _score, item in scored:
        item["tradeoffs"] = _tradeoff_gaps(item["_dims"], item["_measured"], best_by_dim)
        if not include_dims:
            # 私有字段不进生产响应体；include_dims=True 时保留给评测复核
            item.pop("_dims", None)
            item.pop("_measured", None)
            # 2026-10-24 新增的两个私有字段（comfort/intelligence 的原始项数）
            # 同样不能进生产响应体 ⇒ 与上面两个一起剥离。
            item.pop("_comfort_items", None)
            item.pop("_intelligence_items", None)

    top = scored[:limit]
    # 座位约束的**校验覆盖率**：分母是**本次展示的候选数**（top），不是全部候选。
    #
    # ⚠️ 这里曾经用 `len(scored)` 当分母（审查 M2 实测）：12 款候选里 7 款未核实、
    # 只展示 5 张，顶部却说「12 款候选里只有 5 款查到座位数，卡片已标『座位未核实』」
    # ——而那 5 张上**一个标记都没有**，用户去找一个不存在的标记。
    # 用户只看得见展示出来的卡片，所以覆盖率必须按展示口径算；被筛掉的候选
    # 对他不存在，拿它们说事只会让文案与屏幕自相矛盾。
    seat_check = None
    if profile.passengers is not None:
        unverified = sum(1 for _score, item in top if not item["seat_verified"])
        if unverified > 0:
            seat_check = {
                "required": profile.passengers,
                "verified": len(top) - unverified,
                "unverified": unverified,
                "total": len(top),
            }
    return {
        # candidates_scanned = SQL 命中的候选数（= 实际参与评分的款型数）；
        # count = 通过全部硬约束与 Python 侧过滤后真正打分的条数。
        # 两者差距大说明约束把候选挡掉了不少；候选数本身就大则说明是全库扫描。
        "count": len(scored),
        "candidates_scanned": candidates_scanned,
        "variants": [item[1] for item in top],
        "weights_used": weights,
        "seat_check": seat_check,
    }


# ── 评分 v2（AGENT_SCORING_MODE=new）────────────────────────────────────────
def _v2_user_dims(profile: UserProfile) -> set[str]:
    """用户明确提过、且 v2 真的会测量的维度（兼容 v1 的 energy 键）。"""
    dims: set[str] = set()
    for key in (profile.weights or {}):
        mapped = V2_WEIGHT_KEY_ALIASES.get(str(key), str(key))
        if mapped in DIM_LABELS_V2:
            dims.add(mapped)
    return dims


def _v2_matched(
    profile: UserProfile,
    result: dict,
    seats: float | None,
    weights: dict[str, float],
) -> list[str]:
    """v2 的“这台自身达标项”。只写有库内数据、且明显优于同价位中位的维度。"""
    utilities = result.get("utilities") or {}
    measured = result.get("measured") or {}
    candidates = (
        ("safety", "安全配置优于同价位中位"),
        ("running_cost", "用车成本低于同价位中位（按官方参数估算）"),
        ("space", "空间优于同价位中位"),
        ("power", "动力优于同价位中位"),
        ("usage", "用途匹配"),
        ("comfort", "舒适配置优于同价位中位"),
        ("intelligence", "智能配置优于同价位中位"),
    )
    hits: list[tuple[float, str]] = [
        (float(weights.get(dim, 0.0)), label)
        for dim, label in candidates
        if measured.get(dim) and float(utilities.get(dim, 0.0)) >= 0.6
    ]
    hits.sort(key=lambda pair: (-pair[0], pair[1]))
    matched = [label for _weight, label in hits[:4]]
    if profile.passengers is not None and seats is not None and seats >= profile.passengers:
        matched.append(f"座位满足（≥{profile.passengers} 座）")
    return matched


def _recommendation_v2(
    db: Session,
    profile: UserProfile,
    limit: int = 5,
    *,
    include_dims: bool = False,
    exclude_series_ids: Sequence[int] = (),
) -> dict:
    """确定性推荐（v2 七维内核，全部维度按同价位锚点比较）。

    与 v1 的差异只在“怎么打分”：
    - 候选集 SQL 与 v1 共用 _hard_constraint_stmt，逐位等价；
    - 七维：safety/running_cost/space/power/comfort/intelligence/usage；
    - 锚点来自 scoring_anchors_v2.json 的价格带分位，与候选池无关；
    - 缺数据给 0.5 中性并算 coverage，不把“没数据”当“更差”；
    - 排序 U→coverage→销量→variant_id，再按同车系≤2 做多样性收敛。
    """
    anchors = scoring_v2.load_anchors()
    usage_active = scoring_v2.usage_active_for(profile)
    weights = scoring_v2.effective_weights(profile.weights, usage_active=usage_active)

    stmt = _hard_constraint_stmt(profile)
    candidates_scanned = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    if candidates_scanned > _CANDIDATE_SCAN_WARN:
        logging.getLogger("app.agent.recommendation").info(
            json.dumps(
                {
                    "scoring_version": "scoring-v2",
                    "candidates_scanned": candidates_scanned,
                    "limit": limit,
                    "note": "无预算/品牌/锁定约束时退化为全库扫描；加 LIMIT 会改变推荐结果",
                },
                ensure_ascii=False,
            )
        )
    if exclude_series_ids:
        stmt = stmt.where(~VehicleVariant.series_id.in_(list(exclude_series_ids)))
    variants = db.scalars(stmt).all()
    variant_ids = [v.id for v in variants]

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

    fact_rows = (
        db.scalars(
            select(SpecFact).where(
                SpecFact.variant_id.in_(variant_ids),
                or_(
                    SpecFact.fact_key.in_(list(_V2_PARAM_FACT_KEYS)),
                    *[SpecFact.fact_key.like(pattern) for pattern in _V2_FACT_KEY_PATTERNS],
                    SpecFact.category.in_(list(_V2_CONFIG_CATEGORIES)),
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
    _, sales_by_series = _latest_sales_by_series(db, {int(v.series_id) for v in variants})

    scored: list[tuple[float, dict]] = []
    for variant in variants:
        series = series_map.get(variant.series_id)
        if series is None:
            continue
        brand = brand_map.get(series.brand_id)
        fact_list = facts_by_variant.get(variant.id, [])
        facts = {(f["category"], f["fact_key"]): f for f in fact_list}

        price = prices.get(variant.id)
        if price is None:
            continue
        if profile.locked_series_ids and variant.series_id not in profile.locked_series_ids:
            continue
        if profile.budget.min is not None and price < profile.budget.min:
            continue
        if profile.budget.max is not None and price > profile.budget.max:
            continue
        if profile.body_type and variant.body_type not in profile.body_type:
            continue
        if profile.energy_preference:
            allowed = expand_energy_prefs(profile.energy_preference)
            if allowed and variant.energy_type not in allowed:
                continue
        if profile.avoid:
            energy_avoid, body_avoid = expand_avoid(profile.avoid)
            if variant.energy_type in energy_avoid:
                continue
            if variant.body_type and variant.body_type in body_avoid:
                continue
        if profile.rejected_variant_ids and variant.id in profile.rejected_variant_ids:
            continue
        if exclude_series_ids and variant.series_id in exclude_series_ids:
            continue
        seats = _extract_seats(facts)
        if profile.passengers is not None and seats is not None and seats < profile.passengers:
            continue

        features = scoring_v2.extract_features(variant, fact_list, price)
        result = scoring_v2.evaluate(features, profile, anchors)
        score = round(float(result["score"]), 4)
        item = {
            "variant_id": variant.id,
            "series_id": variant.series_id,
            "series_name": series.name,
            "brand_name": brand.name if brand else None,
            "display_name": variant.display_name,
            "energy_type": variant.energy_type,
            "price_cny": price,
            "score": score,
            "matched": _v2_matched(profile, result, seats, weights),
            "tradeoffs": [],
            "seat_verified": profile.passengers is None or seats is not None,
            "coverage": round(float(result["coverage"]), 4),
            "low_evidence": bool(result["low_evidence"]),
            "source_id": variant.source_id,
            "_dims": dict(result["utilities"]),
            "_measured": set(result["measured"]),
            "_parts": dict(result["parts"]),
        }
        scored.append((score, item))

    if _profile_has_no_soft_info(profile):
        for _score, item in scored:
            count = sales_by_series.get(int(item["series_id"]))
            if count is not None:
                item["sales_count"] = count
        scored.sort(
            key=lambda row: (
                -int(row[1].get("sales_count") or 0),
                -round(float(row[0]), 6),
                -float(row[1].get("coverage") or 0.0),
                int(row[1]["variant_id"]),
            )
        )
    else:
        scored = scoring_v2.sort_rows(scored, sales_by_series)

    user_dims = _v2_user_dims(profile) & set(weights)
    best_by_dim: dict[str, float] = {}
    if user_dims:
        for _score, item in scored:
            for dim in item["_measured"]:
                if dim in user_dims:
                    best_by_dim[dim] = max(best_by_dim.get(dim, 0.0), item["_dims"][dim])
    for _score, item in scored:
        item["tradeoffs"] = _tradeoff_gaps(
            item["_dims"], item["_measured"], best_by_dim, DIM_LABELS_V2
        )

    selected, diversity_relaxed = scoring_v2.diversify(scored, limit)
    top = selected

    for _score, item in scored:
        if include_dims:
            item["score_parts"] = item.pop("_parts")
        else:
            item.pop("_dims", None)
            item.pop("_measured", None)
            item.pop("_parts", None)

    seat_check = None
    if profile.passengers is not None:
        unverified = sum(1 for _score, item in top if not item["seat_verified"])
        if unverified > 0:
            seat_check = {
                "required": profile.passengers,
                "verified": len(top) - unverified,
                "unverified": unverified,
                "total": len(top),
            }

    return {
        "count": len(scored),
        "candidates_scanned": candidates_scanned,
        "variants": [item for _score, item in top],
        "weights_used": weights,
        "seat_check": seat_check,
        "scoring_version": "scoring-v2",
        "anchor_version": scoring_v2.anchor_version(anchors),
        "diversity_relaxed": diversity_relaxed,
        "usage_active": usage_active,
    }


def _log_scoring_shadow(
    v1: dict,
    v2: dict,
    profile: UserProfile,
    elapsed_ms: float,
) -> None:
    """shadow 档：只记录 v1/v2 的差异，不改变返回给用户的 v1 结果。"""
    try:
        v1_variants = {int(v["variant_id"]): v for v in v1.get("variants") or []}
        v2_variants = {int(v["variant_id"]): v for v in v2.get("variants") or []}
        common = set(v1_variants) & set(v2_variants)
        top_overlap = len(common) / max(len(v1_variants), len(v2_variants), 1)
        deltas = [
            abs(float(v1_variants[vid]["score"]) - float(v2_variants[vid]["score"]))
            for vid in common
        ]
        payload = {
            "scoring_version": "scoring-v2",
            "profile_hash": hashlib.sha256(
                profile.model_dump_json().encode("utf-8")
            ).hexdigest()[:12],
            "v1_top5": list(v1_variants),
            "v2_top5": list(v2_variants),
            "overlap": round(top_overlap, 3),
            "mean_abs_score_delta": round(sum(deltas) / len(deltas), 4) if deltas else None,
            "v1_count": v1.get("count"),
            "v2_count": v2.get("count"),
            "v2_mean_coverage": (
                round(
                    sum(float(item.get("coverage") or 0.0) for item in v2.get("variants") or [])
                    / max(len(v2.get("variants") or []), 1),
                    4,
                )
            ),
            "v2_low_evidence_rate": (
                round(
                    sum(1 for item in v2.get("variants") or [] if item.get("low_evidence"))
                    / max(len(v2.get("variants") or []), 1),
                    4,
                )
            ),
            "latency_ms": round(elapsed_ms, 1),
        }
        logging.getLogger("app.agent.recommendation").info(
            "scoring_shadow " + json.dumps(payload, ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001 - 影子日志绝不能影响主链
        logging.getLogger("app.agent.recommendation").warning(
            "scoring shadow log failed", exc_info=True
        )


def recommendation_tool(
    db: Session,
    profile: UserProfile,
    limit: int = 5,
    *,
    include_dims: bool = False,
    exclude_series_ids: Sequence[int] = (),
) -> dict:
    """推荐入口：按 AGENT_SCORING_MODE 分派 v1 / v2。

    - off（默认）：逐位沿用 v1 八维内核；
    - new：返回 v2 七维结果；
    - shadow：先算 v1、再算 v2 并记录差异日志，返回 v1。
    开关只影响“怎么打分”，硬约束候选集两版共用同一份 SQL。
    """
    mode = scoring_v2.scoring_mode()
    if mode == "new":
        return _recommendation_v2(
            db, profile, limit, include_dims=include_dims, exclude_series_ids=exclude_series_ids
        )
    if mode == "shadow":
        started = time.perf_counter()
        v1 = _recommendation_v1(
            db, profile, limit, include_dims=include_dims, exclude_series_ids=exclude_series_ids
        )
        v2 = _recommendation_v2(
            db, profile, limit, include_dims=include_dims, exclude_series_ids=exclude_series_ids
        )
        _log_scoring_shadow(v1, v2, profile, (time.perf_counter() - started) * 1000.0)
        return v1
    return _recommendation_v1(
        db, profile, limit, include_dims=include_dims, exclude_series_ids=exclude_series_ids
    )


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
            "description": (
                "查询在售车型系列（可带关键字、车身类型、能源类型、品牌过滤）。只返回数据库中的事实。"
                "询问某品牌有哪些车型时必须用 brand 传品牌名（如「奔驰」），不要凭记忆列举；"
                "需要完整清单时把 limit 调到 200。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "body_type": {"type": "string", "enum": ["sedan", "suv", "mpv"]},
                    "energy_type": {"type": "string"},
                    "brand_id": {"type": "integer"},
                    "brand": {"type": "string", "description": "品牌名（如「奔驰」「比亚迪」），按库内名称/别名解析"},
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
            "description": "返回款型的配置、价格与官方来源，作为事实依据。",
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
            "description": "按用户画像执行硬条件筛选与软评分，返回候选款型。",
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
]
