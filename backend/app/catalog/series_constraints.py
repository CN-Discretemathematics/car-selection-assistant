"""车系约束满足度判定与属性装载（评测规范 v2/v4 与出题共用，单一事实源）。

- series_satisfies(attr, constraints)：检回车系满足问题约束即相关（一题多解合法）；
- load_series_attrs(db, series_ids)：车系约束属性（min_price/能源/车身/最大座位），
  带进程内缓存——流水线 grade 节点按候选车系批量取用，评测一次性全量取用；
- PARAM_KEYS：参数问答键表（键名与数据库 fact_key 对齐），出题/评测/问答兜底三处共用。
"""
from __future__ import annotations

from sqlalchemy import select

from app.common.models import OfficialPrice, SpecFact, VehicleSeries, VehicleVariant

_ATTR_CACHE: dict[int, dict] = {}

# 参数问答键表（键名与数据库 fact_key 对齐；phrase 为用户可读问法）。
# 出题（tools/gen_eval_questions）、评测（tools/eval_rag 的 fact-coverage needle）、
# 问答兜底（app/agent/series_qa 的按键未披露提示）三处共用，单一事实源。
PARAM_KEYS: tuple[tuple[str, str], ...] = (
    ("CLTC纯电续航里程(km)", "CLTC 纯电续航"),
    ("WLTC纯电续航里程(km)", "WLTC 纯电续航"),
    ("WLTC综合油耗(L/100km)", "WLTC 油耗"),
    ("轴距(mm)", "轴距"),
    ("座位数(个)", "座位数"),
    ("最大马力(Ps)", "最大马力"),
    ("电动机总功率(kW)", "电机功率"),
    ("电池能量(kWh)", "电池容量"),
)


def series_satisfies(attr: dict | None, constraints: dict) -> bool:
    """约束满足度判定：检回车系满足问题约束即相关（一题多解合法）。

    支持的约束键：budget_max（元，车系在售最低指导价不超过）、energy_type
    （BEV/PHEV/EREV/HEV/ICE，车系声明能源类型包含）、new_energy（非纯燃油）、
    body_type（suv/sedan/mpv/pickup）、passengers（最大座位数≥N）。
    """
    if not attr:
        return False
    budget = constraints.get("budget_max")
    if budget is not None and (attr["min_price"] is None or attr["min_price"] > budget):
        return False
    energy = constraints.get("energy_type")
    if energy and energy not in attr["energy_types"]:
        return False
    if constraints.get("new_energy") and not (attr["energy_types"] - {"ICE"}):
        return False
    body = constraints.get("body_type")
    if body and attr["body_type"] != body:
        return False
    passengers = constraints.get("passengers")
    if passengers and (attr["max_seats"] is None or attr["max_seats"] < int(passengers)):
        return False
    return True


def load_series_attrs(db, series_ids: set[int] | None = None) -> dict[int, dict]:
    """装载车系约束属性（带缓存）。series_ids 给定时只装这些车系（流水线路径）。"""
    wanted = set(series_ids) if series_ids else None
    missing = wanted - set(_ATTR_CACHE) if wanted else None
    if wanted and not missing:
        return {sid: _ATTR_CACHE[sid] for sid in wanted}

    variants = db.scalars(
        select(VehicleVariant).where(VehicleVariant.status == "on_sale")
    ).all()
    if wanted:
        variants = [v for v in variants if v.series_id in wanted]
    prices: dict[int, float] = {}
    for p in db.scalars(
        select(OfficialPrice).where(OfficialPrice.effective_to.is_(None))
    ).all():
        if wanted and p.variant_id not in {v.id for v in variants}:
            continue
        cur = prices.get(p.variant_id)
        if cur is None or float(p.price_cny) < cur:
            prices[p.variant_id] = float(p.price_cny)
    seats: dict[int, int] = {}
    for vid, value in db.execute(
        select(SpecFact.variant_id, SpecFact.fact_value)
        .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
        .where(VehicleVariant.status == "on_sale", SpecFact.fact_key == "座位数(个)")
    ).all():
        if value and str(value).strip().isdigit():
            seats[vid] = int(value)

    variants_by_series: dict[int, list] = {}
    for v in variants:
        variants_by_series.setdefault(v.series_id, []).append(v)
    out: dict[int, dict] = {}
    for s in db.scalars(
        select(VehicleSeries).where(VehicleSeries.id.in_(wanted)) if wanted else select(VehicleSeries)
    ).all():
        sv = variants_by_series.get(s.id, [])
        sv_seats = [seats[v.id] for v in sv if v.id in seats]
        attr = {
            "name": s.name,
            "brand_id": s.brand_id,
            "body_type": s.body_type,
            "energy_types": set(s.energy_types or []),
            "min_price": min((prices[v.id] for v in sv if v.id in prices), default=None),
            "max_seats": max(sv_seats) if sv_seats else None,
        }
        out[s.id] = attr
        _ATTR_CACHE[s.id] = attr
    return out
