"""车系名称索引与核心参数聚合（目录服务层）。

从消息文本解析真实车系（车系名/品牌+车系名/别名，归一化子串匹配），
并把在售 SKU 事实聚合为「核心参数」一句话画像。供 Agent 问答（series_qa）
与 RAG 流水线（app/rag：查询理解、车系摘要切片）共同复用，避免相互依赖。
"""
from __future__ import annotations

import os
import re
from typing import NamedTuple

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


def keyword_needle(keyword: str | None) -> str:
    """把用户输入的关键词归一化为匹配用 needle（纯空白输入视为未搜索）。"""
    return normalize_name(keyword) if keyword and keyword.strip() else ""


def keyword_score(series: VehicleSeries, brand: Brand, needle: str) -> int | None:
    """关键词匹配得分：越小越靠前；不匹配返回 None。

    归一化后比较（去空白/分隔符 + 小写），因此「腾势Z9 GT」「z9gt」「Z9GT」等效。
    0 = 车系名/别名完全相同；1 = 车系名前缀，或品牌名精确命中（列出该品牌全部车系）；
    2 = 子串命中（车系名/别名/品牌名的任意位置）。

    搜索接口（/vehicles、/home）与 Agent 实体解析共用同一套归一化口径，
    保证「搜得到」与「问得到」一致。
    """
    if not needle:
        return None
    best: int | None = None
    for brand_name in (brand.name, *(brand.aliases or [])):
        normalized = normalize_name(brand_name or "")
        if not normalized:
            continue
        if normalized == needle:
            best = 1 if best is None else min(best, 1)
        elif needle in normalized:
            best = 2 if best is None else min(best, 2)
    names = [series.name, *(series.aliases or [])]
    brand_name = brand.name or ""
    if brand_name and series.name.startswith(brand_name):
        # 车系名自带品牌前缀时，额外登记「去掉品牌名」的短名（海豚 → 比亚迪海豚）
        names.append(series.name[len(brand_name) :])
    for name in names:
        normalized = normalize_name(name or "")
        if not normalized:
            continue
        if normalized == needle:
            score = 0
        elif normalized.startswith(needle):
            score = 1
        elif needle in normalized:
            score = 2
        else:
            continue
        best = score if best is None else min(best, score)
    return best


# 车系名 → series_id 索引缓存（评审 P2：此前每条消息全量加载 908 车系行）。
# 指纹 = (活跃车系数, max(id), max(车系名), max(校验时间), max(品牌名),
#          在售款型数, max(款型 id))；
# pytest 下每个测试都是新建内存库、指纹可能碰撞，直接禁用缓存。
_resolve_cache: dict = {"fingerprint": None, "entries": ()}

# 款型显示名入索引的最短归一化长度（过短如「m5」「pro」跨车系撞名，误配风险大）
_MIN_VARIANT_NAME_LEN = 6


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
            # 2026-10-04：**纯数字名不进对话解析的候选**。
            # 「去掉品牌前缀」会把 领克20 变成 "20"、坦克500 变成 "500"、睿蓝7 变成 "7"…
            # 全库 26 个车系中招。而中文购车语里裸数字几乎总是**预算或年份**：
            #   「我最看重后排空间，预算20万要家用SUV」 → 命中 "20" → 认成 领克20
            #   → decide_route 判 series_qa → **推荐链整条被跳过**，用户只看到一台车的
            #   「官方资料未披露」。实测这不是个例：预算 6/7/8/9/10/11/12/20 万都会中招。
            # 中文没有词边界，靠子串匹配无法把「20万」和「领克20」分开；但反过来，
            # 用户真要问 领克20 时**必然带上品牌**（「领克20」是完整名候选），
            # 砍掉纯数字短名不会伤到任何真实用法，却能消掉整类误伤。
            # ⚠️ 只改对话解析（_load_name_entries）；目录搜索 keyword_score 走另一条
            # 路径、保留裸数字命中——那是用户主动搜「20」，语义与预算无关。
            if len(norm) >= 2 and norm not in seen and not norm.isdigit():
                seen.add(norm)
                entries.append((norm, series.id))
    # 在售款型显示名 → 车系（v6.1）：对比/参数题常以款型名表述（「2023款 470km
    # 引领版」「sDrive25Li X设计套装」），名称索引此前只含车系名/别名，解析器
    # 只能词面模糊误配——compare 题解析准确率仅 34%（27/79）的根因。
    # v6.2（评审 B1）：跨车系撞名的款型名不入索引——生产库实测 103 个归一化款型名
    # 被多车系共享（「2026款 Ultra」→ 理想L6/L8/L9+岚图泰山+智界V9，年份前缀使
    # 长度门槛失效），first-wins 会制造幽灵第二实体把单车系问答误判成对比。
    # 歧义名不携带任何消歧信息，跳过；确定性 ORDER BY 保证可复现。
    variant_rows = db.execute(
        select(VehicleVariant.display_name, VehicleVariant.series_id)
        .join(VehicleSeries, VehicleVariant.series_id == VehicleSeries.id)
        .where(VehicleVariant.status == "on_sale", VehicleSeries.active_status == "active")
        .order_by(VehicleVariant.series_id)
    ).all()
    names_to_series: dict[str, set[int]] = {}
    for display, sid in variant_rows:
        norm = normalize_name(display or "")
        if len(norm) >= _MIN_VARIANT_NAME_LEN:
            names_to_series.setdefault(norm, set()).add(sid)
    series_rows_by_id = {series.id: (series, brand) for series, brand in rows}
    for norm, sids in names_to_series.items():
        if len(sids) == 1:
            sid = next(iter(sids))
            if sid in series_rows_by_id:
                entries.append((norm, sid))
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
    variant_row = db.execute(
        select(
            func.count(VehicleVariant.id),
            func.max(VehicleVariant.id),
            # 评审 minor：改名不改 id/count 时指纹不变会一直服务旧索引
            func.max(VehicleVariant.display_name),
        )
        .join(VehicleSeries, VehicleVariant.series_id == VehicleSeries.id)
        .where(VehicleVariant.status == "on_sale", VehicleSeries.active_status == "active")
    ).one()
    return tuple(row) + tuple(variant_row)


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
    # 去重保序：车系名与其款型名同句出现（「比亚迪e2 的 2023款 出行版」）解析出同一
    # 车系两次会让 analyze 误判为多实体对比——只保留首次命中
    ids = list(dict.fromkeys(c[2] for c in chosen[:6]))[:4]
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


class _Entry(NamedTuple):
    """一条参与极值/区间计算的事实。`text` 是单项展示文本，`raw`/`unit`/`cycle` 供合成区间用。"""

    key: str
    num: float | None
    text: str
    unit: str
    raw: str
    cycle: str


_VALUE_UNIT_RE = re.compile(r"^(.*?)([^\d\s.]+)$")


def _split_embedded_unit(value: str) -> tuple[str, str]:
    """拆出值里自带的单位后缀（「150kW」→「150」「kW」）；拆不出则原样返回。"""
    matched = _VALUE_UNIT_RE.match(value.strip())
    if not matched or not matched.group(1).strip():
        return value.strip(), ""
    return matched.group(1), matched.group(2)


def _range_text(best: _Entry, group: list[_Entry], same_unit: bool) -> str | None:
    """`best` 所在那一组（同 fact_key + 同单位 + 同测试口径）取值 >=2 档时的区间文本。

    返回 None 表示「合不出可信区间」，调用方退回单值——此时输出与改动前逐字一致。

    N9（2026-10-05 实测）：`rank_headlines` 逐 label 取极值，784 个多款型车系里
    **396 个（50.5%）**的核心参数整句没有任何一款型能复现——即
    「汉：续航 705km（来自 3 款 EV）；油耗 0.67L（来自 5 款 DM-i 插混）」，
    这台车在库里根本不存在。单值同样会骗人（小米SU7「最高配续航 902km」实际来自
    中配后驱Pro，顶配四驱Max 为性能牺牲了续航）。改成区间后，展示的每个数都是
    **真实存在过的值**，不会再造出虚构配置；单值车系（min==max）输出逐字不变，
    388/784 个车系零影响。

    **三道安全阀，缺一不可**——每道都对应一类真实存在、但合在一起就错误的量：
      1. 单位一致（否则 1156 Ps 与 850 kW 被写进同一个区间）；
      2. **fact_key 一致**（`CLTC综合续航` 与 `CLTC纯电续航里程` 都是 km+CLTC，
         却是两个不同的量——合成 `125~705 km` 会让用户以为纯电续航能到 705；
         首个版本就漏了这道阀，被自己的测试抓出来了）；
      3. 测试口径一致（WLTC 与 CLTC 的续航不可写进同一个区间）。
    取「极值所在的那一组」而不是全部：汉的 705 来自 `CLTC综合续航`，
    于是输出 `605~705 km（CLTC）`（只含 EV 那三档），DM-i 的 125/245 不参与。
    """
    if not same_unit:
        return None
    cohort = [e for e in group if (e.key, e.unit, e.cycle) == (best.key, best.unit, best.cycle)]
    numeric = [e for e in cohort if e.num is not None]
    if len({e.num for e in numeric}) < 2:
        return None
    lo = min(numeric, key=lambda e: e.num)  # type: ignore[type-var]
    hi = max(numeric, key=lambda e: e.num)  # type: ignore[type-var]
    suffix = f"（{best.cycle}）" if best.cycle else ""
    if best.unit:
        return f"{lo.raw}~{hi.raw} {best.unit}{suffix}"
    # 单位写在值里（如「150kW」）：两端要拆出同一个后缀才拼，否则原样并列
    lo_body, lo_unit = _split_embedded_unit(lo.raw)
    hi_body, hi_unit = _split_embedded_unit(hi.raw)
    if lo_unit and lo_unit == hi_unit:
        return f"{lo_body}~{hi_body}{lo_unit}{suffix}"
    return f"{lo.raw}~{hi.raw}{suffix}"


def rank_headlines(
    facts_by_series: dict[int, list[tuple[str, str, str | None, str | None]]]
) -> dict[int, dict[str, str]]:
    """批量车系核心参数排名（在售事实极值/首值；同单位内比大小，多档给区间）。

    facts_by_series: series_id → [(fact_key, value, unit, cycle), ...]
    供多个调用方复用（车系问答 / RAG 车系摘要切片），避免 N 次单查。

    数值型 label 在同单位同口径内取值 >=2 档时返回 `620~705 km（CLTC）` 这样的区间
    （见 `_range_text`）；单档仍返回单值。`尺寸` 是 text 模式取首值，**不参与区间**——
    长*宽*高 是三元组，合成 `4650~5190*1935*1795` 没有意义（且实测 35.6% 的车系
    尺寸多档时本就只是任取其一，属独立残留问题，不在本函数职责内）。
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
            entries: list[_Entry] = []
            for key in keys:
                for value, unit, cycle in by_key.get(key, []):
                    unit = unit or unit_from_key(key) or ""
                    if unit and value.strip().lower().endswith(unit.lower()):
                        unit = ""  # 值本身已带单位（如「150kW」），避免重复
                    text = f"{value}{f' {unit}' if unit else ''}{f'（{cycle}）' if cycle else ''}"
                    entries.append(
                        _Entry(
                            key=key,
                            num=_numeric(value) if mode != "text" else None,
                            text=text,
                            unit=unit,
                            raw=value,
                            cycle=cycle or "",
                        )
                    )
            if mode == "text":
                if entries:
                    out[label] = entries[0].text
                continue
            numeric = [e for e in entries if e.num is not None]
            if not numeric:
                continue
            # 只在同一单位内比极值：kW 不与 Ps 比大小（防止「1156 Ps > 850 kW」错选）
            ref_unit = numeric[0].unit
            unit_matched = [e for e in numeric if e.unit == ref_unit]
            same_unit = unit_matched or numeric
            best = same_unit[0]
            for entry in same_unit[1:]:
                num = entry.num
                # 评审 m11：生产路径不用 assert（python -O 下会被剥离），显式跳过无数值行
                if num is None:
                    continue
                if mode == "max" and num > best.num:  # type: ignore[operator]
                    best = entry
                elif mode == "min" and num < best.num:  # type: ignore[operator]
                    best = entry
            # 区间优先于单值：N9 修法。极值**选择逻辑未变**，只在能证明同键同单位同口径时
            # 把展示换成区间，取不到区间时 out[label] 与改动前逐字相同。
            out[label] = _range_text(best, same_unit, bool(unit_matched)) or best.text
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
