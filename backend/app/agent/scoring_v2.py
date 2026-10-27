"""评分 v2：七维、固定锚点、全部同价位比较（纯函数）。

设计：docs/scoring-v2-design.md。

本模块**不 import tools/engine**，只依赖标准库与 app.agent.weights，避免循环依赖。
锚点来自 backend/app/agent/scoring_anchors_v2.json（由 tools/gen_scoring_anchors.py 生成），
不在请求内统计候选集——同一款型在任意候选池里分数相同。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.agent.weights import (
    DEFAULT_WEIGHTS_V2,
    PRICE_BANDS_V2,
    SCORING_VERSION_V1,
    SCORING_VERSION_V2,
    USAGE_HINTS,
    V2_WEIGHT_KEY_ALIASES,
)

logger = logging.getLogger("app.agent.scoring_v2")

ANCHOR_VERSION = "scoring-v2-2026-10-03"
ANCHOR_PATH = Path(__file__).with_name("scoring_anchors_v2.json")
ANCHOR_ENV = "SCORING_V2_ANCHORS_PATH"
MODE_ENV = "AGENT_SCORING_MODE"

#: 设计文档里的燃料/电力假设（元/升、元/千瓦时）。集中在这里，评测与文案引用同一份。
FUEL_PRICE_CNY_PER_L = 7.5
ELECTRICITY_PRICE_CNY_PER_KWH = 0.6
PHEV_ELECTRIC_SHARE = 0.5

SAFETY_CATEGORY = "安全配置"
COMFORT_CATEGORIES_V2: tuple[str, ...] = ("内部配置", "外部配置", "操控配置", "舒适性")
INTELLIGENCE_CATEGORIES_V2: tuple[str, ...] = ("智能/辅助驾驶", "智能驾驶", "智能座舱")
USAGE_KEYS: tuple[str, ...] = ("通勤", "家庭", "长途", "商务")

#: 安全配置 10 组关键项。每组在“安全配置”类目下匹配任一 fact_key 且值含 ● 即计 1。
SAFETY_GROUP_PATTERNS: tuple[tuple[str, ...], ...] = (
    ("驾驶座安全气囊", "副驾驶安全气囊", "主/副驾驶座安全气囊", "主/副驾安全气囊", "前排安全气囊"),
    ("前排侧气囊", "侧气囊"),
    ("前/后排头部气囊", "头部气帘", "头部气囊"),
    ("胎压监测", "胎压显示"),
    ("车身稳定控制", "ESP", "ESC", "牵引力控制"),
    ("主动刹车", "主动安全系统", "自动紧急制动"),
    ("前方碰撞预警", "碰撞预警"),
    ("车道偏离预警", "车道保持辅助", "车道居中保持"),
    ("并线辅助", "盲区监测", "盲点监测"),
    ("ISOFIX", "儿童座椅接口"),
)

#: 锚点文件缺失/损坏时的兜底锚点（只用于本地开发/测试，生产必须用生成文件）。
#: 数值按 2026-10-03 生产快照的量级取整，具体见 design §1.3；正式锚点以 JSON 为准。
FALLBACK_ANCHORS: dict[str, dict[str, dict[str, float]]] = {
    "space": {
        "wheelbase": {"p10": 2550.0, "p50": 2750.0, "p90": 2950.0},
        "length": {"p10": 4300.0, "p50": 4650.0, "p90": 5000.0},
        "width": {"p10": 1700.0, "p50": 1830.0, "p90": 1950.0},
        "trunk": {"p10": 300.0, "p50": 450.0, "p90": 600.0},
    },
    "power": {"kw_per_t": {"p10": 60.0, "p50": 90.0, "p90": 140.0}},
    "running_cost": {"cny_per_100km": {"p10": 30.0, "p50": 55.0, "p90": 90.0}},
    "safety": {"count": {"p10": 3.0, "p50": 6.0, "p90": 9.0}},
    "comfort": {"count": {"p10": 5.0, "p50": 15.0, "p90": 30.0}},
    "intelligence": {"count": {"p10": 2.0, "p50": 8.0, "p90": 16.0}},
    "usage": {
        "length": {"p10": 4300.0, "p50": 4650.0, "p90": 5000.0},
        "trunk": {"p10": 300.0, "p50": 450.0, "p90": 600.0},
        "range": {"p10": 300.0, "p50": 500.0, "p90": 700.0},
        "intelligence": {"p10": 2.0, "p50": 8.0, "p90": 16.0},
    },
}

_ANCHOR_CACHE: dict[str, dict[str, Any]] = {}


# ── 模式与版本 ────────────────────────────────────────────────────────────────
def scoring_mode() -> str:
    """AGENT_SCORING_MODE=off|shadow|new；未知值回退 off（fail safe）。"""
    raw = (os.getenv(MODE_ENV) or "off").strip().lower()
    if raw in ("off", "shadow", "new"):
        return raw
    logger.warning("unknown %s=%r, fall back to off", MODE_ENV, raw)
    return "off"


def uses_v2(mode: str | None = None) -> bool:
    """当前模式返回给用户的结果是否真是 v2（用于标签/提示词/追问的语义切换）。

    与 is_v2_mode 的区别：shadow 也会算 v2，但返回的是 v1，所以面向用户的
    文案与提示词必须仍按 v1 口径，否则会出现「提示词说有安全维、分数却是 v1」。
    """
    return (mode or scoring_mode()) == "new"


def is_v2_mode(mode: str | None = None) -> bool:
    return (mode or scoring_mode()) in ("shadow", "new")


def scoring_version(mode: str | None = None) -> str:
    return SCORING_VERSION_V2 if is_v2_mode(mode) else SCORING_VERSION_V1


# ── 锚点加载 ──────────────────────────────────────────────────────────────────
def clear_anchor_cache() -> None:
    _ANCHOR_CACHE.clear()


def load_anchors(path: str | Path | None = None) -> dict[str, Any] | None:
    """加载版本化锚点；缺失/损坏返回 None，由调用方用 FALLBACK_ANCHORS 兜底。"""
    resolved = Path(path or os.getenv(ANCHOR_ENV) or ANCHOR_PATH)
    key = str(resolved)
    if key in _ANCHOR_CACHE:
        return _ANCHOR_CACHE[key]
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("scoring anchors not found: %s", resolved)
        return None
    except (OSError, ValueError) as exc:
        logger.warning("scoring anchors unreadable: %s (%s)", resolved, exc)
        return None
    if not isinstance(data, dict) or "global" not in data:
        logger.warning("scoring anchors malformed: %s", resolved)
        return None
    _ANCHOR_CACHE[key] = data
    return data


def anchor_version(anchors: Mapping[str, Any] | None = None) -> str:
    if anchors:
        return str(anchors.get("anchor_version") or ANCHOR_VERSION)
    return ANCHOR_VERSION


def price_band_index(price_cny: float | None) -> int | None:
    if price_cny is None:
        return None
    try:
        price_wan = float(price_cny) / 10000.0
    except (TypeError, ValueError):
        return None
    for idx, (low, high) in enumerate(PRICE_BANDS_V2):
        if price_wan >= low and (high is None or price_wan < high):
            return idx
    return len(PRICE_BANDS_V2) - 1


def _band_anchor_block(anchors: Mapping[str, Any] | None, price_cny: float | None) -> Mapping[str, Any] | None:
    idx = price_band_index(price_cny)
    if idx is None or not anchors:
        return None
    block = (anchors.get("by_price_band") or {}).get(str(idx))
    if not isinstance(block, dict) or block.get("fallback"):
        return None
    inner = block.get("anchors")
    return inner if isinstance(inner, dict) else None


def anchor_for(
    anchors: Mapping[str, Any] | None,
    price_cny: float | None,
    dim: str,
    subkey: str,
) -> Mapping[str, float] | None:
    """取“同价位锚点”：该价格带样本足够就用带内锚点，否则回退全库锚点。"""
    band = _band_anchor_block(anchors, price_cny)
    candidates: list[Mapping[str, Any]] = []
    if band:
        candidates.append(band)
    if anchors:
        global_anchors = anchors.get("global")
        if isinstance(global_anchors, dict):
            candidates.append(global_anchors)
    for block in candidates:
        dim_block = block.get(dim)
        if isinstance(dim_block, dict):
            anchor = dim_block.get(subkey)
            if isinstance(anchor, dict):
                return anchor
    fallback = FALLBACK_ANCHORS.get(dim, {}).get(subkey)
    return fallback


# ── 基础数学 ──────────────────────────────────────────────────────────────────
def tri(x: float | None, p10: float, p50: float, p90: float) -> float | None:
    """分段线性映射：x≤p10→0，p50→0.5，p90→1；锚点退化时返回 0.5。"""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not (p10 < p50 < p90):
        return 0.5
    if x <= p10:
        return 0.0
    if x <= p50:
        return 0.5 * (x - p10) / (p50 - p10)
    if x <= p90:
        return 0.5 + 0.5 * (x - p50) / (p90 - p50)
    return 1.0


def anchor_utility(
    value: float | None,
    anchor: Mapping[str, float] | None,
    *,
    higher_better: bool = True,
) -> float | None:
    if value is None:
        return None
    if not anchor:
        return 0.5
    value_tri = tri(value, float(anchor["p10"]), float(anchor["p50"]), float(anchor["p90"]))
    if value_tri is None:
        return None
    return value_tri if higher_better else 1.0 - value_tri


# ── 事实解析 ──────────────────────────────────────────────────────────────────
def _normalise_facts(facts: Any) -> list[dict[str, Any]]:
    if not facts:
        return []
    if isinstance(facts, Mapping):
        iterable = list(facts.values())
    else:
        iterable = list(facts)
    out: list[dict[str, Any]] = []
    for raw in iterable:
        if not isinstance(raw, Mapping):
            continue
        value = raw.get("value", raw.get("fact_value"))
        out.append(
            {
                "category": str(raw.get("category") or ""),
                "fact_key": str(raw.get("fact_key") or ""),
                "value": value,
                "unit": raw.get("unit"),
                "cycle": raw.get("cycle"),
            }
        )
    return out


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group(0)) if match else None


def _numeric_entries(facts_by_key: Mapping[str, list[dict[str, Any]]], key: str) -> list[tuple[float, dict[str, Any]]]:
    out: list[tuple[float, dict[str, Any]]] = []
    for fact in facts_by_key.get(key, []):
        num = _parse_number(fact.get("value"))
        if num is not None:
            out.append((num, fact))
    return out


def _pick_alias(
    facts_by_key: Mapping[str, list[dict[str, Any]]],
    aliases: Sequence[str],
) -> tuple[float, dict[str, Any]] | None:
    """按优先级取第一个键；同一键多值冲突（值不同）则跳过该键。"""
    for key in aliases:
        entries = _numeric_entries(facts_by_key, key)
        if not entries:
            continue
        distinct = {round(num, 6) for num, _ in entries}
        if len(distinct) > 1:
            continue
        return entries[0]
    return None


def _ordered_keys_by_cycle(
    facts_by_key: Mapping[str, list[dict[str, Any]]],
    *,
    contains: str,
    preferred_cycles: Sequence[str],
) -> tuple[float, dict[str, Any]] | None:
    grouped: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for key in facts_by_key:
        if contains not in key:
            continue
        entries = _numeric_entries(facts_by_key, key)
        if entries:
            grouped[key] = entries
    if not grouped:
        return None
    cycles = tuple(preferred_cycles)
    ranked: list[tuple[int, str]] = []
    for key in grouped:
        cycle = str((grouped[key][0][1].get("cycle") or "")).upper()
        priority = len(cycles)
        for idx, preferred in enumerate(cycles):
            if preferred.upper() in cycle or preferred.upper() in key.upper():
                priority = idx
                break
        ranked.append((priority, key))
    ranked.sort()
    for _priority, key in ranked:
        entries = grouped[key]
        distinct = {round(num, 6) for num, _ in entries}
        if len(distinct) > 1:
            continue
        return entries[0]
    return None


def _dimensions_from_value(value: Any) -> tuple[float | None, float | None]:
    text = str(value or "")
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))]
    if len(nums) >= 3:
        return nums[0], nums[1]
    if len(nums) == 1:
        return nums[0], None
    return None, None


def _pick_fuel(
    facts_by_key: Mapping[str, list[dict[str, Any]]], energy_type: str
) -> tuple[float, dict[str, Any]] | None:
    preferred = ("WLTC", "NEDC", "CLTC")
    if energy_type in ("PHEV", "EREV"):
        preferred = ("最低荷电状态",) + preferred
    return _ordered_keys_by_cycle(facts_by_key, contains="油耗", preferred_cycles=preferred)


def _pick_electricity(
    facts_by_key: Mapping[str, list[dict[str, Any]]],
) -> tuple[float, dict[str, Any]] | None:
    return _ordered_keys_by_cycle(facts_by_key, contains="耗电量", preferred_cycles=("CLTC", "WLTC", "NEDC"))


def _pick_range(
    facts_by_key: Mapping[str, list[dict[str, Any]]],
) -> tuple[float, dict[str, Any]] | None:
    return _pick_alias(
        facts_by_key,
        (
            "CLTC综合续航(km)",
            "CLTC综合续航里程(km)",
            "CLTC纯电续航里程(km)",
            "WLTC综合续航(km)",
            "WLTC纯电续航里程(km)",
            "NEDC综合续航(km)",
            "NEDC纯电续航里程(km)",
            "续航里程(km)",
            "续航(km)",
        ),
    )


def _pick_power_kw(facts_by_key: Mapping[str, list[dict[str, Any]]]) -> tuple[float, dict[str, Any]] | None:
    found = _pick_alias(
        facts_by_key,
        ("系统综合功率(kW)", "电动机总功率(kW)", "最大功率(kW)"),
    )
    if found:
        return found
    hp = _pick_alias(facts_by_key, ("最大马力(Ps)", "最大马力(ps)"))
    if hp:
        value, fact = hp
        return value * 0.73549875, fact
    return None


def _parse_facts(facts: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for fact in _normalise_facts(facts):
        out.setdefault(fact["fact_key"], []).append(fact)
    return out


def _config_counts(facts: list[dict[str, Any]]) -> dict[str, Any]:
    safety_hits = [False] * len(SAFETY_GROUP_PATTERNS)
    safety_fact_count = 0
    comfort_count = 0
    comfort_fact_count = 0
    intelligence_count = 0
    intelligence_fact_count = 0
    for fact in facts:
        category = fact["category"]
        key = fact["fact_key"]
        is_standard = "●" in str(fact.get("value") or "")
        if category == SAFETY_CATEGORY:
            safety_fact_count += 1
            if is_standard:
                for idx, patterns in enumerate(SAFETY_GROUP_PATTERNS):
                    if any(pattern.lower() in key.lower() for pattern in patterns):
                        safety_hits[idx] = True
        if category in COMFORT_CATEGORIES_V2:
            comfort_fact_count += 1
            if is_standard:
                comfort_count += 1
        if category in INTELLIGENCE_CATEGORIES_V2:
            intelligence_fact_count += 1
            if is_standard:
                intelligence_count += 1
    return {
        "safety_count": sum(1 for hit in safety_hits if hit),
        "safety_fact_count": safety_fact_count,
        "comfort_count": comfort_count,
        "comfort_fact_count": comfort_fact_count,
        "intelligence_count": intelligence_count,
        "intelligence_fact_count": intelligence_fact_count,
    }


def extract_features(variant: Any, facts: Any, price_cny: float | None) -> dict[str, Any]:
    """把一款型的库内事实转成 v2 原始特征；不在这里做任何候选集统计。"""
    fact_list = _normalise_facts(facts)
    by_key = _parse_facts(fact_list)
    energy_type = str(getattr(variant, "energy_type", "") or "")
    seat_count = getattr(variant, "seat_count", None)
    if seat_count is None:
        seats = _pick_alias(by_key, ("座位数(个)", "座位数", "seats"))
        seat_count = seats[0] if seats else None

    wheelbase = _pick_alias(by_key, ("轴距(mm)", "轴距"))
    length_fact = _pick_alias(by_key, ("长*宽*高(mm)", "长度(mm)", "车长(mm)"))
    width_fact = _pick_alias(by_key, ("宽度(mm)",))
    length = None
    width = None
    if length_fact:
        parsed_length, parsed_width = _dimensions_from_value(length_fact[1].get("value"))
        length = parsed_length
        width = parsed_width
    if width is None and width_fact:
        width = width_fact[0]
    trunk = _pick_alias(by_key, ("后备厢容积(L)", "后备厢容积", "行李厢容积(L)"))

    curbs = _pick_alias(by_key, ("整备质量(kg)", "整备质量"))
    power_kw = _pick_power_kw(by_key)
    fuel = _pick_fuel(by_key, energy_type)
    electricity = _pick_electricity(by_key)
    range_km = _pick_range(by_key)
    counts = _config_counts(fact_list)

    curb_kg = curbs[0] if curbs else None
    kw_per_t = None
    if power_kw and curb_kg and curb_kg > 0:
        kw_per_t = power_kw[0] / curb_kg * 1000.0

    cost = None
    cost_basis = None
    if energy_type in ("ICE", "HEV"):
        if fuel:
            cost = fuel[0] * FUEL_PRICE_CNY_PER_L
            cost_basis = "fuel"
    elif energy_type == "BEV":
        if electricity:
            cost = electricity[0] * ELECTRICITY_PRICE_CNY_PER_KWH
            cost_basis = "electric"
    elif energy_type in ("PHEV", "EREV"):
        if fuel and electricity:
            cost = (
                PHEV_ELECTRIC_SHARE * electricity[0] * ELECTRICITY_PRICE_CNY_PER_KWH
                + (1.0 - PHEV_ELECTRIC_SHARE) * fuel[0] * FUEL_PRICE_CNY_PER_L
            )
            cost_basis = "mixed"
        elif fuel:
            cost = fuel[0] * FUEL_PRICE_CNY_PER_L
            cost_basis = "fuel"
        elif electricity:
            cost = electricity[0] * ELECTRICITY_PRICE_CNY_PER_KWH
            cost_basis = "electric"
    else:
        if fuel:
            cost = fuel[0] * FUEL_PRICE_CNY_PER_L
            cost_basis = "fuel"
        elif electricity:
            cost = electricity[0] * ELECTRICITY_PRICE_CNY_PER_KWH
            cost_basis = "electric"

    return {
        "price_cny": price_cny,
        "energy_type": energy_type,
        "body_type": getattr(variant, "body_type", None),
        "seat_count": seat_count,
        "space": {
            "wheelbase": wheelbase[0] if wheelbase else None,
            "length": length,
            "width": width,
            "trunk": trunk[0] if trunk else None,
        },
        "power": {
            "power_kw": power_kw[0] if power_kw else None,
            "curb_weight_kg": curb_kg,
            "kw_per_t": kw_per_t,
        },
        "running_cost": {"cny_per_100km": cost, "basis": cost_basis},
        **counts,
        "range_km": range_km[0] if range_km else None,
        "evidence": {
            "space": {
                "wheelbase": wheelbase[1] if wheelbase else None,
                "length": length_fact[1] if length_fact else None,
                "trunk": trunk[1] if trunk else None,
            },
            "power": power_kw[1] if power_kw else None,
            "fuel": fuel[1] if fuel else None,
            "electricity": electricity[1] if electricity else None,
            "range": range_km[1] if range_km else None,
        },
    }


# ── 七维效用 ──────────────────────────────────────────────────────────────────
def _u(anchors: Mapping[str, Any] | None, price: float | None, dim: str, subkey: str, value: float | None, *, higher_better: bool = True) -> float:
    utility = anchor_utility(value, anchor_for(anchors, price, dim, subkey), higher_better=higher_better)
    return 0.5 if utility is None else utility


def _space_utility(features: Mapping[str, Any], anchors: Mapping[str, Any] | None) -> tuple[float, bool]:
    space = features.get("space") or {}
    price = features.get("price_cny")
    parts: list[tuple[float, float]] = []
    if space.get("wheelbase") is not None:
        parts.append((0.45, _u(anchors, price, "space", "wheelbase", space["wheelbase"])))
    if space.get("length") is not None:
        parts.append((0.30, _u(anchors, price, "space", "length", space["length"])))
    if space.get("width") is not None:
        parts.append((0.15, _u(anchors, price, "space", "width", space["width"])))
    if space.get("trunk") is not None:
        parts.append((0.10, _u(anchors, price, "space", "trunk", space["trunk"])))
    if not parts:
        return 0.5, False
    total = sum(weight for weight, _ in parts)
    return sum(weight * utility for weight, utility in parts) / total, True


def _power_utility(features: Mapping[str, Any], anchors: Mapping[str, Any] | None) -> tuple[float, bool]:
    value = (features.get("power") or {}).get("kw_per_t")
    if value is None:
        return 0.5, False
    return _u(anchors, features.get("price_cny"), "power", "kw_per_t", value), True


def _cost_utility(features: Mapping[str, Any], anchors: Mapping[str, Any] | None) -> tuple[float, bool]:
    value = (features.get("running_cost") or {}).get("cny_per_100km")
    if value is None:
        return 0.5, False
    return _u(anchors, features.get("price_cny"), "running_cost", "cny_per_100km", value, higher_better=False), True


def _config_utility(
    features: Mapping[str, Any],
    anchors: Mapping[str, Any] | None,
    dim: str,
    count_key: str,
    fact_key: str,
    minimum_facts: int,
) -> tuple[float, bool]:
    fact_count = int(features.get(fact_key) or 0)
    if fact_count < minimum_facts:
        return 0.5, False
    return _u(anchors, features.get("price_cny"), dim, "count", float(features.get(count_key) or 0)), True


def _normalise_usage(profile_usage: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw in profile_usage or []:
        key = USAGE_HINTS.get(str(raw), str(raw))
        if key in USAGE_KEYS and key not in out:
            out.append(key)
    return out


def _usage_one(
    usage: str,
    features: Mapping[str, Any],
    anchors: Mapping[str, Any] | None,
) -> float:
    price = features.get("price_cny")
    energy = str(features.get("energy_type") or "")
    body = str(features.get("body_type") or "")
    parts: list[float] = []
    if usage == "通勤":
        parts.append(1.0 if energy in ("BEV", "PHEV", "HEV") else (0.0 if energy == "ICE" else 0.5))
        parts.append({"sedan": 1.0, "hatchback": 0.8, "suv": 0.5, "mpv": 0.0}.get(body, 0.5))
        length = (features.get("space") or {}).get("length")
        if length is not None:
            parts.append(1.0 - _u(anchors, price, "usage", "length", length))
        else:
            parts.append(0.5)
    elif usage == "家庭":
        parts.append(1.0 if body in ("suv", "mpv") else (0.5 if body == "sedan" else 0.0))
        seats = features.get("seat_count")
        parts.append(0.5 if seats is None else (1.0 if float(seats) >= 5 else 0.0))
        trunk = (features.get("space") or {}).get("trunk")
        parts.append(_u(anchors, price, "usage", "trunk", trunk) if trunk is not None else 0.5)
    elif usage == "长途":
        parts.append(0.4 if energy == "BEV" else (1.0 if energy in ("ICE", "HEV", "PHEV", "EREV") else 0.5))
        range_km = features.get("range_km")
        parts.append(_u(anchors, price, "usage", "range", range_km) if range_km is not None else 0.5)
        trunk = (features.get("space") or {}).get("trunk")
        parts.append(_u(anchors, price, "usage", "trunk", trunk) if trunk is not None else 0.5)
    elif usage == "商务":
        parts.append({"sedan": 1.0, "mpv": 1.0, "suv": 0.3}.get(body, 0.5))
        length = (features.get("space") or {}).get("length")
        parts.append(_u(anchors, price, "usage", "length", length) if length is not None else 0.5)
        intelligence = features.get("intelligence_count")
        if intelligence is not None:
            parts.append(_u(anchors, price, "usage", "intelligence", float(intelligence)))
        else:
            parts.append(0.5)
    else:
        return 0.5
    return sum(parts) / len(parts) if parts else 0.5


def usage_active_for(profile: Any) -> bool:
    """画像里是否有可识别的用途（决定 usage 维是否激活）。"""
    return bool(_normalise_usage(getattr(profile, "usage", []) or []))


def _usage_utility(
    features: Mapping[str, Any],
    profile: Any,
    anchors: Mapping[str, Any] | None,
) -> tuple[float, bool]:
    usages = _normalise_usage(getattr(profile, "usage", []) or [])
    if not usages:
        return 0.5, False
    return sum(_usage_one(usage, features, anchors) for usage in usages) / len(usages), True


# ── 聚合与排序 ────────────────────────────────────────────────────────────────
def effective_weights(profile_weights: Mapping[str, float] | None, *, usage_active: bool) -> dict[str, float]:
    """v2 有效权重：默认值 + profile 绝对覆盖（兼容 v1 键），再归一化。"""
    raw = dict(DEFAULT_WEIGHTS_V2)
    for key, value in (profile_weights or {}).items():
        if value is None:
            continue
        mapped = V2_WEIGHT_KEY_ALIASES.get(str(key), str(key))
        if mapped not in raw:
            continue
        try:
            raw[mapped] = max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    if not usage_active:
        raw.pop("usage", None)
    total = sum(raw.values())
    if total <= 0:
        fallback = {key: value for key, value in DEFAULT_WEIGHTS_V2.items() if usage_active or key != "usage"}
        total = sum(fallback.values())
        raw = fallback
    return {key: value / total for key, value in raw.items() if value > 0}


def aggregate(
    utilities: Mapping[str, float],
    measured: Mapping[str, bool],
    weights: Mapping[str, float],
) -> dict[str, Any]:
    total_weight = sum(weights.get(dim, 0.0) for dim in utilities if dim in weights)
    if total_weight <= 0:
        return {"score": 0.5, "coverage": 0.0, "score_low": 0.0, "score_high": 1.0, "low_evidence": True}
    weighted = 0.0
    measured_weight = 0.0
    low = 0.0
    high = 0.0
    parts: dict[str, float] = {}
    for dim, utility in utilities.items():
        weight = float(weights.get(dim, 0.0))
        if weight <= 0:
            continue
        is_measured = bool(measured.get(dim, False))
        effective = float(utility) if is_measured else 0.5
        weighted += weight * effective
        low += weight * (float(utility) if is_measured else 0.0)
        high += weight * (float(utility) if is_measured else 1.0)
        if is_measured:
            measured_weight += weight
        parts[dim] = round(weight * effective, 6)
    score = weighted / total_weight
    coverage = measured_weight / total_weight
    return {
        "score": score,
        "coverage": coverage,
        "score_low": low / total_weight,
        "score_high": high / total_weight,
        "low_evidence": coverage < 0.5,
        "parts": parts,
    }


def evaluate(
    features: Mapping[str, Any],
    profile: Any,
    anchors: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """计算一款型的七维效用、有效权重、总分、coverage 与证据。"""
    if anchors is None:
        anchors = load_anchors()
    space_u, space_m = _space_utility(features, anchors)
    power_u, power_m = _power_utility(features, anchors)
    cost_u, cost_m = _cost_utility(features, anchors)
    safety_u, safety_m = _config_utility(
        features, anchors, "safety", "safety_count", "safety_fact_count", minimum_facts=5
    )
    comfort_u, comfort_m = _config_utility(
        features, anchors, "comfort", "comfort_count", "comfort_fact_count", minimum_facts=1
    )
    intelligence_u, intelligence_m = _config_utility(
        features, anchors, "intelligence", "intelligence_count", "intelligence_fact_count", minimum_facts=1
    )
    usage_u, usage_active = _usage_utility(features, profile, anchors)

    utilities: dict[str, float] = {
        "safety": safety_u,
        "running_cost": cost_u,
        "space": space_u,
        "power": power_u,
        "comfort": comfort_u,
        "intelligence": intelligence_u,
    }
    measured: dict[str, bool] = {
        "safety": safety_m,
        "running_cost": cost_m,
        "space": space_m,
        "power": power_m,
        "comfort": comfort_m,
        "intelligence": intelligence_m,
    }
    if usage_active:
        utilities["usage"] = usage_u
        measured["usage"] = True
    weights = effective_weights(getattr(profile, "weights", None), usage_active=usage_active)
    agg = aggregate(utilities, measured, weights)
    cost_ev = (features.get("running_cost") or {}).get("cny_per_100km")
    return {
        "utilities": utilities,
        "measured": measured,
        "weights": weights,
        "score": agg["score"],
        "coverage": agg["coverage"],
        "score_low": agg["score_low"],
        "score_high": agg["score_high"],
        "low_evidence": agg["low_evidence"],
        "parts": agg["parts"],
        "usage_active": usage_active,
        "evidence": {
            "cost_cny_per_100km": round(cost_ev, 2) if cost_ev is not None else None,
            "cost_basis": (features.get("running_cost") or {}).get("basis"),
            "kw_per_t": round(float((features.get("power") or {}).get("kw_per_t")), 2)
            if (features.get("power") or {}).get("kw_per_t") is not None
            else None,
            "wheelbase_mm": (features.get("space") or {}).get("wheelbase"),
            "length_mm": (features.get("space") or {}).get("length"),
            "trunk_l": (features.get("space") or {}).get("trunk"),
            "safety_items": features.get("safety_count"),
            "comfort_items": features.get("comfort_count"),
            "intelligence_items": features.get("intelligence_count"),
            "range_km": features.get("range_km"),
        },
    }


def sort_rows(
    rows: Sequence[tuple[float, dict[str, Any]]],
    sales_by_series: Mapping[int, int] | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    """v2 确定性排序：U→coverage→最新销量→variant_id（升序）。"""
    sales = sales_by_series or {}

    def key(row: tuple[float, dict[str, Any]]) -> tuple[float, float, int, int]:
        score, item = row
        return (
            -round(float(score), 6),
            -round(float(item.get("coverage") or 0.0), 6),
            -int(sales.get(int(item.get("series_id") or 0), item.get("sales_count") or 0) or 0),
            int(item.get("variant_id") or 0),
        )

    return sorted(rows, key=key)


def diversify(
    rows: Sequence[tuple[float, dict[str, Any]]],
    limit: int,
) -> tuple[list[tuple[float, dict[str, Any]]], int]:
    """确定性多样性：同车系≤2；不够则放宽到≤3；再不够则不限。返回 (选中行, 放宽级别)。"""
    target = min(int(limit), len(rows))
    if target <= 0:
        return [], 0
    for level, cap in ((0, 2), (1, 3), (2, None)):
        selected: list[tuple[float, dict[str, Any]]] = []
        counts: dict[int, int] = {}
        for row in rows:
            series_id = int(row[1].get("series_id") or 0)
            if cap is not None and counts.get(series_id, 0) >= cap:
                continue
            selected.append(row)
            counts[series_id] = counts.get(series_id, 0) + 1
            if len(selected) >= target:
                return selected, level
    return list(rows[:target]), 2
