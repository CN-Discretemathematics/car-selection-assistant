"""车系名称索引与核心参数聚合（目录服务层）。

从消息文本解析真实车系（车系名/品牌+车系名/别名，归一化子串匹配），
并把在售 SKU 事实聚合为「核心参数」一句话画像。供 Agent 问答（series_qa）
与 RAG 流水线（app/rag：查询理解、车系摘要切片）共同复用，避免相互依赖。
"""
from __future__ import annotations

import os
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.models import Brand, SpecFact, VehicleSeries, VehicleVariant

_SEP_RE = re.compile(r"[\s\-—–·、.。:：/\\()]+")

# 每个车系的「核心参数」优先级键（按顺序取，同量纲取极值）
HEADLINE_SPECS: list[tuple[str, tuple[str, ...], str]] = [
    ("尺寸", ("长*宽*高(mm)",), "text"),
    ("轴距", ("轴距(mm)",), "max"),
    ("动力", ("电动机总功率(kW)", "系统综合功率(kW)", "电动机总马力(Ps)", "最大功率(kW)"), "max"),
    ("续航", ("CLTC综合续航(km)", "CLTC纯电续航里程(km)", "WLTC纯电续航里程(km)"), "max"),
    ("油耗", ("WLTC综合油耗(L/100km)", "最低荷电状态油耗(L/100km)WLTC",
              "最低荷电状态油耗(L/100km)NEDC", "油电综合燃料消耗量(L/100km)"), "min"),
    ("加速", ("官方0-100km/h加速(s)",), "min"),
    ("电池", ("电池能量(kWh)",), "max"),
]
HEADLINE_ORDER: tuple[str, ...] = tuple(label for label, _, _ in HEADLINE_SPECS)


def normalize_name(text: str) -> str:
    """归一化车系名/品牌名：去空白与分隔符、小写（腾势Z9 GT → 腾势z9gt）。"""
    return _SEP_RE.sub("", text).lower()


# 车系名 → series_id 索引缓存（评审 P2：此前每条消息全量加载 908 车系行）。
# 指纹 = (活跃车系数, max(id), max(车系名), max(校验时间), max(品牌名))；
# pytest 下每个测试都是新建内存库、指纹可能碰撞，直接禁用缓存。
_resolve_cache: dict = {"fingerprint": None, "entries": ()}


def _load_name_entries(db: Session) -> tuple[tuple[str, int], ...]:
    rows = db.execute(
        select(VehicleSeries, Brand)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(VehicleSeries.active_status == "active")
    ).all()
    entries: list[tuple[str, int]] = []
    for series, brand in rows:
        names: list[str] = [series.name]
        brand_name = brand.name if brand else ""
        if brand_name and series.name.startswith(brand_name):
            names.append(series.name[len(brand_name):])
        if brand_name:
            names.append(f"{brand_name}{series.name}")
        names.extend(list(series.aliases or []))
        seen: set[str] = set()
        for name in names:
            norm = normalize_name(name)
            if len(norm) >= 2 and norm not in seen:
                seen.add(norm)
                entries.append((norm, series.id))
    return tuple(entries)


def _series_fingerprint(db: Session) -> tuple:
    row = db.execute(
        select(
            func.count(VehicleSeries.id),
            func.max(VehicleSeries.id),
            func.max(VehicleSeries.name),
            func.max(VehicleSeries.last_verified_at),
            func.max(Brand.name),
        )
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(VehicleSeries.active_status == "active")
    ).one()
    return tuple(row)


def resolve_series(db: Session, message: str) -> list[tuple[VehicleSeries, Brand | None]]:
    """从消息中解析用户提及的真实车系（按出现顺序，最多 4 个）。

    匹配候选 = 车系名 / 品牌+车系名 / 去掉品牌前缀的车系名 / 别名；
    归一化后做子串匹配，重叠名字优先保留更长的（「腾势Z9」让位「腾势Z9GT」）。
    """
    msg = normalize_name(message)
    if not msg:
        return []
    if "PYTEST_CURRENT_TEST" in os.environ:
        entries = _load_name_entries(db)
    else:
        fingerprint = _series_fingerprint(db)
        if _resolve_cache["fingerprint"] != fingerprint:
            _resolve_cache["fingerprint"] = fingerprint
            _resolve_cache["entries"] = _load_name_entries(db)
        entries = _resolve_cache["entries"]

    candidates: list[tuple[str, int]] = [(norm, sid) for norm, sid in entries if norm in msg]
    chosen: list[tuple[int, int, int, str]] = []  # (start, end, series_id, norm)
    for norm, sid in sorted(candidates, key=lambda c: (-len(c[0]), c[1])):
        start = msg.find(norm)
        end = start + len(norm)
        if any(start < oend and end > ostart for ostart, oend, _, _ in chosen):
            continue  # 与已选更长名字重叠（腾势Z9 ⊂ 腾势Z9GT）
        chosen.append((start, end, sid, norm))
    chosen.sort(key=lambda c: c[0])
    ids = [c[2] for c in chosen[:4]]
    if not ids:
        return []
    rows = db.execute(
        select(VehicleSeries, Brand)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(VehicleSeries.id.in_(ids))
    ).all()
    by_id = {series.id: (series, brand) for series, brand in rows}
    return [by_id[sid] for sid in ids if sid in by_id]


def display_name(series: VehicleSeries, brand: Brand | None) -> str:
    if brand and brand.name and not series.name.startswith(brand.name):
        return f"{brand.name}{series.name}"
    return series.name


def _numeric(text: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


_KEY_UNIT_RE = re.compile(r"\(([^()]*)\)[^()]*$")
_UNIT_CHARS = re.compile(r"^[A-Za-z0-9%·²³/°]+$")


def unit_from_key(key: str) -> str | None:
    """从键末括号提取单位（如「电动机总功率(kW)」「WLTC综合油耗(L/100km)」→ kW / L/100km）：
    事实行 unit 为空的兜底；键尾带工况文本（如「…(L/100km)WLTC」）也兼容。"""
    match = _KEY_UNIT_RE.search(key)
    if not match:
        return None
    candidate = match.group(1)
    return candidate if _UNIT_CHARS.match(candidate) else None


def rank_headlines(
    facts_by_series: dict[int, list[tuple[str, str, str | None, str | None]]]
) -> dict[int, dict[str, str]]:
    """批量车系核心参数排名（在售事实极值/首值；同单位内比大小）。

    facts_by_series: series_id → [(fact_key, value, unit, cycle), ...]
    供多个调用方复用（车系问答 / RAG 车系摘要切片），避免 N 次单查。
    """
    out_map: dict[int, dict[str, str]] = {}
    for sid, rows in facts_by_series.items():
        by_key: dict[str, list[tuple[str, str | None, str | None]]] = {}
        for key, value, unit, cycle in rows:
            if not value:
                continue
            by_key.setdefault(key, []).append((value, unit, cycle))

        out: dict[str, str] = {}
        for label, keys, mode in HEADLINE_SPECS:
            entries: list[tuple[str, float | None, str, str]] = []  # (key, num, text, unit)
            for key in keys:
                for value, unit, cycle in by_key.get(key, []):
                    unit = unit or unit_from_key(key) or ""
                    if unit and value.strip().lower().endswith(unit.lower()):
                        unit = ""  # 值本身已带单位（如「150kW」），避免重复
                    text = f"{value}{f' {unit}' if unit else ''}{f'（{cycle}）' if cycle else ''}"
                    entries.append((key, _numeric(value) if mode != "text" else None, text, unit))
            if mode == "text":
                if entries:
                    out[label] = entries[0][2]
                continue
            numeric = [e for e in entries if e[1] is not None]
            if not numeric:
                continue
            # 只在同一单位内比极值：kW 不与 Ps 比大小（防止「1156 Ps > 850 kW」错选）
            ref_unit = numeric[0][3]
            same_unit = [e for e in numeric if e[3] == ref_unit] or numeric
            best = same_unit[0]
            for entry in same_unit[1:]:
                num = entry[1]
                # 评审 m11：生产路径不用 assert（python -O 下会被剥离），显式跳过无数值行
                if num is None:
                    continue
                if mode == "max" and num > best[1]:  # type: ignore[operator]
                    best = entry
                elif mode == "min" and num < best[1]:  # type: ignore[operator]
                    best = entry
            out[label] = best[2]
        out_map[sid] = out
    return out_map


def series_headline(db: Session, series: VehicleSeries) -> dict[str, str]:
    """车系核心参数（在售 SKU 事实中的极值/首值），用于问答与对比。"""
    rows = db.execute(
        select(SpecFact.fact_key, SpecFact.fact_value, SpecFact.unit, SpecFact.cycle)
        .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
        .where(VehicleVariant.series_id == series.id, VehicleVariant.status == "on_sale")
    ).all()
    return rank_headlines({series.id: [(r[0], r[1], r[2], r[3]) for r in rows]})[series.id]


def series_fact_rows(
    db: Session, series_ids: list[int]
) -> dict[int, list[tuple[str, str, str | None, str | None]]]:
    """批量取多个车系的在售 SKU 事实行（series_id → [(key, value, unit, cycle)]）。"""
    facts_by_series: dict[int, list[tuple[str, str, str | None, str | None]]] = {}
    if not series_ids:
        return facts_by_series
    for key, value, unit, cycle, sid in db.execute(
        select(
            SpecFact.fact_key, SpecFact.fact_value, SpecFact.unit, SpecFact.cycle,
            VehicleVariant.series_id,
        )
        .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.series_id.in_(series_ids),
            VehicleVariant.status == "on_sale",
        )
    ).all():
        facts_by_series.setdefault(sid, []).append((key, value, unit, cycle))
    return facts_by_series
