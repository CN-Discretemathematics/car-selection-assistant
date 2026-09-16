"""对比差异分析：把「参数罗列」变成「决策相关的差异与取舍」。（2026-09-15）

用户反馈：对比页的「分析差异」只是把参数原样列一遍，没有真正的分析。

设计原则（与 PROJECT_PLAN §21「不编造数据」一致）：
- **只使用库内事实**：每个数值都带 `source_id` 与原文键名，分析结论是这些事实的**确定性推导**
  （比较、差值、阈值判定），不做任何推测；
- **只呈现有决策意义的差异**：每维声明 `threshold`（如功率 15kW、续航 50km），低于阈值记
  「无明显差异」而不是把两位小数都摆出来；
- **明确信息缺口**：某维度缺数据的款型列入 `gaps`，文案统一「官方资料未披露」；
- 维度键全部来自对生产库 `spec_facts` 的实测（覆盖率见各维度注释），不做假设。

术语：款型（variant）= 用户可购买的具体配置；车系（series）= 车型。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agent.tools import vehicle_evidence
from app.common.enums import MISSING_VALUE_LABEL, NEW_ENERGY_TYPES

# 从事实键的括号里兜底取单位（库里部分事实的 unit 字段为空，单位写在键名里，如「后备厢容积(L)」）
_UNIT_IN_KEY_RE = re.compile(r"[（(]([^）)]{1,14})[)）]\s*$")
# 单值数字：整段只有一个数字才算（避免把「150-200」「5/7」这类区间误读成单一值）
_SINGLE_NUMBER_RE = re.compile(r"^[^0-9]*(-?\d+(?:\.\d+)?)[^0-9]*$")
# 布尔型配置的「有/无」表达
# 汽车之家配置表约定：●=标配 ○=选配 —=无（全角「－」与半角「—」都是“无”的写法，
# 第三轮复审 m：原先把全角「－」放进 _TRUE_WORDS 是笔误）
_TRUE_WORDS = ("●", "有", "标配", "是", "支持")
_FALSE_WORDS = ("○", "无", "不配备", "否", "不支持", "-", "—", "－")
_PRESENT = "有"
_ABSENT = "无"

# 单位换算：同维度内允许的等价单位 → 换算到规范单位的系数
# 实测坑（2026-09-15）：同车系不同年款的快充时间分别写「0.68 小时」与「25.4 分钟」，
# 不做换算会得出「低 24.72 小时」这种荒谬结论——那等于编造数据。
_UNIT_CONVERSIONS: dict[str, dict[str, float]] = {
    "fast_charge": {"小时": 60.0, "h": 60.0, "分钟": 1.0, "min": 1.0},
}


@dataclass(frozen=True)
class Dimension:
    """一个决策维度：命中哪些事实键、越大越好还是越小越好、多大差距才算「有差异」。"""

    key: str
    label: str
    patterns: tuple[str, ...]
    better: str = "higher"          # higher | lower | none（none = 只呈现差异，不判优劣）
    threshold: float | None = None  # 数值维度的显著差异阈值（同维度单位）
    kind: str = "number"            # number | flag（有无型配置）
    why: str = ""                   # 业务含义（写给用户看的短句，不含任何数据）
    canonical_unit: str = ""        # 规范单位（不同写法换算后统一，避免跨单位比较）
    exclude: tuple[str, ...] = ()   # 命中这些子串的事实键一律排除（如分电机功率）

    def matches(self, fact_key: str) -> bool:
        if not any(pat in fact_key for pat in self.patterns):
            return False
        return not any(pat in fact_key for pat in self.exclude)


# 维度定义（键名均为生产库实测存在的键；覆盖率写在注释里）
DIMENSIONS: tuple[Dimension, ...] = (
    # 动力/扭矩必须排除「前/后电动机」分项键：子串匹配会命中它们（双电机款型
    # 会被取到分电机值而非整车值，★ 判定失真——第三轮复审 BLOCKED）
    Dimension("power", "动力（最大功率）", ("最大功率(kW)",), "higher", 15.0,
              why="决定加速与高速再加速能力",
              exclude=("前电动机", "后电动机")),
    Dimension("torque", "扭矩", ("最大扭矩(N·m)",), "higher", 30.0,
              why="决定起步与爬坡的推力感",
              exclude=("前电动机", "后电动机")),
    Dimension("range", "纯电续航", ("CLTC纯电续航里程", "WLTC纯电续航里程"), "higher", 50.0,
              why="决定日常通勤能否纯电覆盖（覆盖约 44% 款型）"),
    Dimension("range_total", "综合续航", ("CLTC综合续航", "WLTC综合续航"), "higher", 80.0,
              why="决定满油满电的长途能力"),
    Dimension("fuel", "综合油耗", ("WLTC综合油耗(L/100km)", "NEDC综合油耗(L/100km)"), "lower", 0.8,
              why="决定馈电/纯燃油状态的使用成本（数值越低越省）"),
    Dimension("battery", "电池容量", ("电池能量(kWh)",), "higher", 5.0,
              why="与纯电续航、快充能力相关"),
    Dimension("fast_charge", "快充时间", ("电池快充时间",), "lower", 5.0,
              why="决定补能便利性（统一换算为分钟，数值越低越快）", canonical_unit="分钟"),
    Dimension("wheelbase", "轴距", ("轴距(mm)",), "higher", 50.0,
              why="直接影响后排腿部空间"),
    Dimension("length", "车长", ("长度(mm)",), "higher", 100.0,
              why="影响后备厢纵深与第三排可用性"),
    Dimension("trunk", "后备厢容积", ("后备厢容积(L)",), "higher", 60.0,
              why="直接影响载物能力（覆盖约 55% 款型）"),
    Dimension("mass", "整备质量", ("整备质量(kg)",), "lower", 100.0,
              why="影响能耗与灵活性（越轻越省）"),
    Dimension("top_speed", "最高车速", ("最高车速(km/h)",), "higher", 20.0,
              why="高速巡航余量"),
    Dimension("air_susp", "空气悬架", ("空气悬架",), kind="flag",
              why="影响滤震质感与车身高度调节"),
    # 侧气帘：生产库真实键为「前/后排头部气囊(气帘)」（覆盖 4,946 款型，84%）——
    # 原模式（侧气帘/侧安全气帘）在生产数据上零命中，属于维度定义错误（第三轮复审 M）
    Dimension("side_airbag", "头部气帘", ("前/后排头部气囊(气帘)",), kind="flag",
              why="侧碰时保护前后排乘员头部"),
    # 自适应巡航：生产库无稳定覆盖的键（仅 n=2 的「自适应巡航包」），如实移除该维度
    # 而不是放一个永远空的维度（第三轮复审 M）
)


@dataclass
class _Variant:
    variant_id: int
    label: str
    price: float | None
    energy_type: str | None
    facts: dict[str, dict] = field(default_factory=dict)
    # 同名参数在库内存在多个不同取值（如混动车的「最大功率(kW)」既有系统综合 144
    # 又有发动机净功率 116）→ key → 去重后的取值列表。这类维度必须标注存疑、不参与比较：
    # 任选一行都会凭空造出差异（2026-09-16 实测事故：卡罗拉锐放两款实际同为 144kW，
    # 却因两行覆盖顺序不同得出「144 vs 116、差 24%」）。
    conflicting_keys: dict[str, list[str]] = field(default_factory=dict)

    @property
    def short(self) -> str:
        return self.label


def _variant_label(evidence: dict) -> str:
    """款型展示名：品牌 + 车系 + 款型名（与前端一致）。"""
    parts = [evidence.get("brand_name"), evidence.get("series_name"), evidence.get("display_name")]
    return " ".join(p for p in parts if p)


def _unit_of(fact: dict) -> str | None:
    unit = (fact.get("unit") or "").strip()
    if unit:
        return unit
    match = _UNIT_IN_KEY_RE.search(fact.get("fact_key") or "")
    return match.group(1) if match else None


def _to_number(fact: dict) -> float | None:
    """解析单值数字；区间/多值/文本一律返回 None（宁可不比，也不误读）。"""
    match = _SINGLE_NUMBER_RE.match((fact.get("value") or "").strip())
    return float(match.group(1)) if match else None


def _to_flag(fact: dict) -> str | None:
    value = (fact.get("value") or "").strip()
    if not value:
        return None
    if value.startswith("选配"):
        return "选配"
    if value in _TRUE_WORDS or value.startswith(_TRUE_WORDS):
        return _PRESENT
    if value in _FALSE_WORDS or value.startswith(_FALSE_WORDS):
        return _ABSENT
    # 文本型配置（如「空气悬架」写的是类型名）→ 视为具备
    return value[:12]


def _pick_fact(facts: dict[str, dict], dim: Dimension) -> dict | None:
    """同维度可能有多个键（如 CLTC/WLTC 两套续航、快充时间的「分钟」与「小时」两个键）。

    取值偏好：① 单位与维度规范单位一致的键（免换算）；② CLTC 优先；③ 首个有值的。
    """
    candidates = [fact for key, fact in facts.items() if dim.matches(key) and (fact.get("value") or "").strip()]
    if not candidates:
        return None

    def sort_key(fact: dict) -> tuple[int, int]:
        unit = _unit_of(fact) or ""
        unit_match = 0 if (dim.canonical_unit and unit == dim.canonical_unit) else 1
        cycle_match = 0 if "CLTC" in (fact.get("fact_key") or "") else 1
        return (unit_match, cycle_match)

    candidates.sort(key=sort_key)
    return candidates[0]


def analyze_comparison(db: Session, variant_ids: list[int]) -> dict:
    """对 2~5 个款型做确定性差异分析，返回结构化结论（全部来自库内事实）。"""
    evidence = [vehicle_evidence(db, vid) for vid in variant_ids]
    rows = [e for e in evidence if "error" not in e]
    if len(rows) < 2:
        return {"error": "至少需要 2 个有效款型才能对比分析"}

    variants: list[_Variant] = []
    for row in rows:
        by_key: dict[str, list[dict]] = {}
        for fact in row.get("facts") or []:
            by_key.setdefault(str(fact.get("fact_key") or ""), []).append(fact)
        facts: dict[str, dict] = {}
        conflicting_keys: dict[str, list[str]] = {}
        for key, group in by_key.items():
            # 同键多行：按（单位、取值）稳定排序后取首个 —— 结果不依赖 DB 返回顺序
            # （原实现直接 dict 覆盖，取值随行序漂移，正是假差异的来源）
            group.sort(key=lambda f: (_unit_of(f) or "", str(f.get("value") or "")))
            facts[key] = group[0]
            distinct = sorted({str(f.get("value") or "").strip() for f in group if str(f.get("value") or "").strip()})
            if len(distinct) > 1:
                conflicting_keys[key] = distinct
        variants.append(
            _Variant(
                variant_id=row["variant_id"],
                label=_variant_label(row),
                price=row.get("official_price"),
                energy_type=row.get("energy_type"),
                facts=facts,
                conflicting_keys=conflicting_keys,
            )
        )

    dimensions: list[dict] = []
    gaps: list[dict] = []
    leaders: dict[int, list[str]] = {v.variant_id: [] for v in variants}
    trails: dict[int, list[str]] = {v.variant_id: [] for v in variants}
    allowed_numbers: set[float] = set()

    for dim in DIMENSIONS:
        picked = {v.variant_id: _pick_fact(v.facts, dim) for v in variants}

        # ① 同键多值冲突（库内同名参数有两个不同取值）：只列示、不比较——
        #    任选一行都会造出假差异，宁可标注存疑并让用户看到冲突本身
        suspect = {
            v.variant_id: v.conflicting_keys.get(str((picked[v.variant_id] or {}).get("fact_key") or ""))
            for v in variants
        }
        if any(suspect.values()):
            note = "库内同一参数存在多个不同取值（" + "；".join(
                f"{v.label}：{' / '.join(suspect[v.variant_id] or [])}"
                for v in variants if suspect[v.variant_id]
            ) + "），已标注存疑、不参与比较"
            values = []
            for v in variants:
                fact = picked[v.variant_id]
                conflicting = suspect[v.variant_id]
                if conflicting:
                    display = f"存疑（{' / '.join(conflicting)}）"
                elif fact:
                    display = f"{str(fact.get('value') or '').strip()}{_unit_of(fact) or ''}"
                else:
                    display = MISSING_VALUE_LABEL
                values.append({
                    "variant_id": v.variant_id,
                    "display": display,
                    "raw": None,
                    "leader": False,
                    "source_id": (fact or {}).get("source_id"),
                    "fact_key": (fact or {}).get("fact_key"),
                })
            dimensions.append({
                "key": dim.key, "label": dim.label, "why": dim.why, "values": values,
                "significant": False, "gap": None, "note": note,
            })
            continue

        missing = [v.label for v in variants if picked[v.variant_id] is None]
        if len(missing) == len(variants):
            continue  # 全体都没有这个维度 → 整个维度不呈现（不是缺口，是没这项数据）
        if missing:
            gaps.append({"dimension": dim.label, "missing": missing, "note": MISSING_VALUE_LABEL})

        values: list[dict] = []
        if dim.kind == "flag":
            for v in variants:
                fact = picked[v.variant_id]
                flag = _to_flag(fact) if fact else None
                values.append({
                    "variant_id": v.variant_id,
                    "display": flag or MISSING_VALUE_LABEL,
                    "raw": None,
                    "leader": False,
                    "source_id": (fact or {}).get("source_id"),
                    "fact_key": (fact or {}).get("fact_key"),
                })
            present_ids = [val["variant_id"] for val in values if val["display"] == _PRESENT]
            partial_ids = [val["variant_id"] for val in values if val["display"] == "选配"]
            significant = 0 < len(present_ids) < len(values) or bool(partial_ids)
            for val in values:
                val["leader"] = val["variant_id"] in present_ids and significant
            gap_text = None
            if significant and present_ids:
                names = [v.label for v in variants if v.variant_id in present_ids]
                gap_text = f"仅 {('、'.join(names))} 配备"
        else:
            numeric: dict[int, float] = {}
            incomparable = False
            for v in variants:
                fact = picked[v.variant_id]
                number = _to_number(fact) if fact else None
                unit = _unit_of(fact) if fact else None
                canonical = dim.canonical_unit or unit
                display_unit = canonical or unit
                if number is not None and dim.canonical_unit and unit and unit != dim.canonical_unit:
                    factor = _UNIT_CONVERSIONS.get(dim.key, {}).get(unit)
                    if factor is None:
                        incomparable = True  # 单位不可换算 → 该维度只列示，不比较
                    else:
                        # 换算后展示「原值 ≈ 规范值」，避免用未换算的原值误导（实测踩过：
                        # 0.68 小时被当作 0.68 分钟 → 得出「低 24.72 小时」的荒谬结论）
                        raw_display = str((fact or {}).get("value") or "").strip()
                        original_unit = unit
                        number = round(number * factor, 2)
                        unit = dim.canonical_unit
                        values.append({
                            "variant_id": v.variant_id,
                            "display": f"{raw_display}{original_unit} ≈ {number:g}{unit}",
                            "raw": number,
                            "leader": False,
                            "source_id": (fact or {}).get("source_id"),
                            "fact_key": (fact or {}).get("fact_key"),
                        })
                        numeric[v.variant_id] = number
                        continue
                if number is not None and not incomparable:
                    numeric[v.variant_id] = number
                display = (fact or {}).get("value") or MISSING_VALUE_LABEL
                show_unit = display_unit and str(display_unit) not in str(display)
                values.append({
                    "variant_id": v.variant_id,
                    "display": f"{display} {display_unit}" if show_unit else str(display),
                    "raw": number,
                    "leader": False,
                    "source_id": (fact or {}).get("source_id"),
                    "fact_key": (fact or {}).get("fact_key"),
                })
            if incomparable:
                dimensions.append({
                    "key": dim.key, "label": dim.label, "why": dim.why,
                    "values": values, "significant": False, "gap": None,
                    "note": "各单位写法不一致且无法换算，仅列示原始数据（不做比较）",
                })
                continue
            if len(numeric) < 2:
                if numeric or missing:
                    dimensions.append({
                        "key": dim.key, "label": dim.label, "why": dim.why,
                        "values": values, "significant": False, "gap": None,
                        "note": "可比数值不足，仅列示原始数据",
                    })
                continue
            # 工况一致性（第三轮复审 BLOCKED）：CLTC/WLTC/NEDC 不可直接比较——
            # 各款型取到的fact 若工况不同（键名含不同工况词），只列示并标注，不输出差值
            cycles = {
                next((c for c in ("CLTC", "WLTC", "NEDC") if c in (picked[v.variant_id] or {}).get("fact_key", "")), "")
                for v in variants
                if v.variant_id in numeric
            }
            if len(cycles) > 1:
                dimensions.append({
                    "key": dim.key, "label": dim.label, "why": dim.why,
                    "values": values, "significant": False, "gap": None,
                    "note": "各款型工况不同（" + "/".join(sorted(c for c in cycles if c)) + "），不直接比较",
                })
                continue
            best_id = (max if dim.better == "higher" else min)(numeric, key=lambda k: numeric[k])
            worst_id = (min if dim.better == "higher" else max)(numeric, key=lambda k: numeric[k])
            delta = abs(numeric[best_id] - numeric[worst_id])
            significant = dim.threshold is None or delta >= dim.threshold
            for val in values:
                val["leader"] = significant and val["variant_id"] == best_id
                if val["raw"] is not None:
                    allowed_numbers.add(round(float(val["raw"]), 3))
            allowed_numbers.add(round(delta, 3))
            # gap 文案用**规范单位**：用领先者 fact 的原单位会在换算场景下输出错误单位
            # （第三轮复审 BLOCKED：「0.68小时 ≈ 40.8分钟」的对比却写「低 13.4小时」）
            unit = dim.canonical_unit or _unit_of(picked[best_id] or {}) or ""
            gap_text = None
            if significant:
                leader = next(v for v in variants if v.variant_id == best_id)
                trailer = next(v for v in variants if v.variant_id == worst_id)
                ratio_value = round(delta / numeric[worst_id] * 100) if numeric[worst_id] else None
                ratio = f"，差距 {ratio_value:.0f}%" if ratio_value is not None else ""
                if ratio_value is not None:
                    allowed_numbers.add(float(ratio_value))  # 差距百分比同样进白名单
                gap_text = f"{leader.label} 比 {trailer.label} {dim.better == 'higher' and '高' or '低'} {delta:g}{unit}{ratio}"
            if significant:
                leaders[best_id].append(dim.label)
                trails[worst_id].append(dim.label)

        dimensions.append({
            "key": dim.key, "label": dim.label, "why": dim.why,
            "values": values, "significant": significant, "gap": gap_text, "note": None,
        })

    # 价格（单独处理：既是成本也是「贵在哪」的锚点）
    prices = {v.variant_id: v.price for v in variants if v.price is not None}
    price_row: dict | None = None
    if len(prices) >= 2:
        lo_id = min(prices, key=lambda k: prices[k])
        hi_id = max(prices, key=lambda k: prices[k])
        delta = prices[hi_id] - prices[lo_id]
        if delta:
            allowed_numbers.add(round(delta, 3))
            allowed_numbers.add(round(delta / 10000, 3))
        price_row = {
            "key": "price", "label": "官方指导价", "why": "对比的基准；差价要看换来了哪些领先项",
            "values": [
                {
                    "variant_id": v.variant_id,
                    "display": f"{v.price / 10000:.2f} 万元" if v.price is not None else MISSING_VALUE_LABEL,
                    "raw": v.price,
                    "leader": v.variant_id == lo_id,
                }
                for v in variants
            ],
            "significant": delta >= 10000,
            "gap": (f"{next(v.label for v in variants if v.variant_id == hi_id)} 比 "
                    f"{next(v.label for v in variants if v.variant_id == lo_id)} 贵 {delta / 10000:.2f} 万元"
                    if delta >= 10000 else None),
            "note": None,
        }
        for v in variants:
            if v.price is not None:
                allowed_numbers.add(round(float(v.price), 3))
                allowed_numbers.add(round(float(v.price) / 10000, 3))

    # 取舍与建议（模板化，句子里出现的数字都已登记在 allowed_numbers）
    tradeoffs: list[str] = []
    for v in variants:
        wins, losses = leaders.get(v.variant_id, []), trails.get(v.variant_id, [])
        if not wins and not losses:
            continue
        price_note = ""
        if len(prices) >= 2 and v.price is not None:
            cheapest = min(prices.values())
            if v.price == max(prices.values()) and v.price > cheapest:
                price_note = f"，但指导价最高（{v.price / 10000:.2f} 万元）"
            elif v.price == cheapest:
                price_note = f"，且指导价最低（{v.price / 10000:.2f} 万元）"
        win_note = f"在 {'、'.join(wins)} 上领先" if wins else ""
        lose_note = f"在 {'、'.join(losses)} 上落后" if losses else ""
        body = "；".join(x for x in (win_note, lose_note) if x) or "各维度差异不显著"
        tradeoffs.append(f"{v.label}：{body}{price_note}")

    summary: list[str] = []
    if price_row:
        summary.append(f"共对比 {len(variants)} 个款型；{price_row['gap'] or '指导价差异不足 1 万元'}")
    for dim in dimensions:
        if dim["significant"] and dim["gap"]:
            summary.append(f"{dim['label']}：{dim['gap']}")
    if gaps:
        summary.append("信息缺口：" + "、".join(g["dimension"] for g in gaps) + f"（{MISSING_VALUE_LABEL}）")
    if not any(dim["significant"] for dim in dimensions):
        summary.append("本次对比的维度差异均不显著（低于各自阈值），建议按价格与售后网络做取舍")

    label_by_id = {v.variant_id: v.label for v in variants}
    verdict = _build_verdict(variants, leaders, prices)
    key_points = _build_key_points(dimensions, label_by_id)

    return {
        "variants": [
            {
                "variant_id": v.variant_id,
                "label": v.label,
                "price": v.price,
                "energy_type": v.energy_type,
                "is_new_energy": (v.energy_type in NEW_ENERGY_TYPES) if v.energy_type else None,
                "leaders": leaders.get(v.variant_id, []),
                "trails": trails.get(v.variant_id, []),
            }
            for v in variants
        ],
        "price": price_row,
        "dimensions": dimensions,
        "tradeoffs": tradeoffs,
        "summary": summary,
        "gaps": gaps,
        # 一句话结论 + 关键差异 Top3：给「先看结论」的用户（细节仍在 dimensions/summary）
        "verdict": verdict,
        "key_points": key_points,
        # 供答案数字校验：回答里出现的数字必须落在这个集合内（防止模型编数字）
        "allowed_numbers": sorted(allowed_numbers),
    }


def _build_verdict(
    variants: list, leaders: dict[int, list[str]], prices: dict[int, float]
) -> str | None:
    """一句话结论（确定性模板，不含任何库外事实）：谁强在哪 + 谁最便宜。

    款型全名较长，两个款型时价格子句用「前者/后者」指代，避免整句被全名撑爆。
    """
    parts: list[str] = []
    for v in variants:
        wins = leaders.get(v.variant_id, [])[:2]
        if wins:
            parts.append(f"{v.label} 强在 {'、'.join(wins)}")
    if not parts:
        return None
    verdict = "总体：" + "；".join(parts)
    if len(prices) >= 2:
        cheapest_id = min(prices, key=lambda k: prices[k])
        cheapest = next(v for v in variants if v.variant_id == cheapest_id)
        if len(variants) == 2:
            ordinal = "前者" if cheapest_id == variants[0].variant_id else "后者"
            verdict += f"；{ordinal}指导价最低"
        else:
            verdict += f"；{cheapest.label} 指导价最低"
    return verdict + "。"


def _build_key_points(dimensions: list[dict], label_by_id: dict[int, str]) -> list[dict]:
    """关键差异 Top3：显著维度按「差距百分比」从大到小（解析不到百分比的排后面）。"""
    scored: list[tuple[float, int, dict, str]] = []
    for idx, dim in enumerate(dimensions):
        if not (dim.get("significant") and dim.get("gap")):
            continue
        match = re.search(r"差距 ([0-9]+(?:\.[0-9]+)?)%", dim["gap"])
        leader = next((v for v in dim["values"] if v.get("leader")), None)
        if leader is None:
            continue
        pct = float(match.group(1)) if match else -1.0
        scored.append((pct, idx, dim, label_by_id.get(leader["variant_id"], "")))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [
        {"label": dim["label"], "winner": winner, "gap": dim["gap"]}
        for _, _, dim, winner in scored[:3]
    ]


def render_analysis_text(analysis: dict) -> str:
    """把分析结果渲染成确定性文案（LLM 不可用时的兜底，与 LLM 版同源同数据）。"""
    if "error" in analysis:
        return analysis["error"]
    lines: list[str] = []
    if analysis.get("verdict"):
        lines.append(analysis["verdict"])   # 先给结论，再给明细
    lines.extend(analysis.get("summary") or [])
    if analysis.get("tradeoffs"):
        lines.append("取舍：")
        lines.extend(f"- {t}" for t in analysis["tradeoffs"])
    return "\n".join(lines)
