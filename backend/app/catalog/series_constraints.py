"""车系约束满足度判定、属性装载与文本约束解析（评测 v2/v4 与出题/流水线共用，单一事实源）。

- series_satisfies(attr, constraints)：检回车系满足问题约束即相关（一题多解合法）；
- parse_budget_and_seats(text) / parse_energy_body(text)：从文本反解结构化约束
  （正则与 engine.extract_hints 同源——本模块为正则的唯一定义处，engine 导入复用）；
- load_series_attrs(db, series_ids)：车系约束属性（min_price/能源/车身/最大座位），
  指纹化进程内缓存（评审 C3/C4：旧实现每冷启动车系做全表扫描且永不失效；
  指纹覆盖车系/款型/价格/座位数据变化，pytest 下禁用缓存防跨测试污染）；
- PARAM_KEYS：参数问答键表（键名与数据库 fact_key 对齐），出题/评测/问答兜底三处共用。
"""
from __future__ import annotations

import os
import re

from sqlalchemy import func, select

from app.common.models import OfficialPrice, SpecFact, VehicleSeries, VehicleVariant

# ── 预算/座位文本解析（与 engine.extract_hints 同源的正则，唯一定义处）─────────
BUDGET_RANGE_BOTH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*[-~到至]\s*(\d+(?:\.\d+)?)\s*万")  # 20万到30万
BUDGET_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~到至]\s*(\d+(?:\.\d+)?)\s*万")  # 20-30万
BUDGET_MAX_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*(?:以内|以下|之内|内)")
BUDGET_MIN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*(?:以上|起步|起)")
# 「20多万」「30万出头」→ 下限口径
BUDGET_MIN_LOOSE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万?\s*(?:多万|出头|大几万|往上)")
# 裸「N万」仅在预算语境（「预算N万」）下视为预算上限——「销量30万」不得误判
BUDGET_BARE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万")
BUDGET_CONTEXT_RE = re.compile(r"预算|落地|价位|价格")
# 座位/人数解析。**必须区分闭开区间**。
#
# 2026-10-05 生产实测修：原 `PASSENGERS_RE` 只取数字、把「以上」整个丢掉，于是
# 「5人以上」与「3~5人」双双解析成 5 座——那个选项因此**不提供任何新信息**，
# 且系统会把 5 座车推给明确要 6 人以上的用户（站点实测：「5人以上」→ 首推两款
# 5 座车，卡片写「座位满足（≥5 座）」）。
#
# 两条分支的「以上」口径**刻意不同**，别顺手统一：
# - 人数侧（「N 人以上」）= 用户说自己家有几个人，按**开区间**读 → 座位下限 N+1。
#   这是 `_passenger_options` 递给用户点的那个选项，**定义权在产品**（用户 2026-10-05
#   拍板「算 6 座」）。
# - 座位侧（「N 座以上」）= 用户在描述车本身的配置，按**含端点**读（5 座含 5 座）。
#   若也 +1，用户说「找台5座以上的」会被反向多要一个座位，砍掉真正符合的车。
#
# ⚠️ 人数侧的 +1 **与同文件预算侧的口径相反**，这是有意的：预算侧「20 万以上」取
# 下限 20（含端点，`BUDGET_MIN_RE`），因为钱是连续量；人数侧的「5 人以上」是产品
# 定义的档位标签，取「超过 5 人」。别拿「与预算侧对齐」当理由把这里改成含端点——
# 那是把用户拍板的口径改回去。中文「5 人以上」的字面标准义确实含端点，这里取开区间
# 属**产品选择**而非语言学必然。
_PASSENGERS_PEOPLE_RE = re.compile(
    r"([一二两三四五六七八九十\d]+)\s*(?:个|口)?\s*人\s*(以上|开以上|或以上|以内|以下|之内|内)?"
)
_PASSENGERS_SEATS_RE = re.compile(
    r"([一二两三四五六七八九十\d]+)\s*座\s*(以上|开以上|或以上|以内|以下|之内|内)?"
)
#: 人数侧出现这些后缀时按**开区间**读（「超过 N 人」），座位下限 +1。
#: 必须覆盖正则里所有「以上」写法（审查 L1 实测：原先只有「以上」，
#: 「5 人开以上」「5 人或以上」会漏掉 +1，同一语义三种写法两种结果）。
_PEOPLE_OPEN_BOUND_SUFFIXES = ("以上", "开以上", "或以上")
_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def parse_passengers(text: str) -> int | None:
    """人数/座位文本 → 座位数下限。**座位解析的唯一实现**。

    曾经只有一个 `PASSENGERS_RE`，两个调用点（`parse_budget_and_seats` 与
    `engine.extract_hints`）各自 `.search()` + 取组号。这正是本次事故的结构性
    原因：**开区间规则没有单一归属，谁都能写出「只取数字」的版本**，于是 5 座车
    被推给要 6 人以上的用户。现在规则与匹配收在同一个函数里，调用点无法再绕开。

    人数分支与座位分支同时命中时取**文本中靠前**的那个（沿用旧正则「谁先出现谁
    生效」的行为，不在这里悄悄改语义）。
    """
    people = _PASSENGERS_PEOPLE_RE.search(text or "")
    seats = _PASSENGERS_SEATS_RE.search(text or "")
    if people and (not seats or people.start() <= seats.start()):
        token, suffix, is_people = people.group(1), people.group(2), True
    elif seats:
        token, suffix, is_people = seats.group(1), seats.group(2), False
    else:
        return None
    n = _cn_int(token or "")
    if not n:
        return None
    # +1 **只作用于人数分支**（见模块注释：开区间 = 超过 N 人）。
    # 座位分支「6 座以上」是用户在描述车，按**含端点**读；若也 +1 就会把
    # 「找台 6 座以上的」反向变成要 7 座，砍掉真正符合的车。
    if is_people and suffix in _PEOPLE_OPEN_BOUND_SUFFIXES:
        n += 1
    return n


def _cn_int(token: str) -> int | None:
    try:
        return int(token)
    except ValueError:
        pass
    total, num = 0, 0
    for ch in token:
        if ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        else:
            return None
    return total + num


def parse_budget_and_seats(text: str) -> dict:
    """预算（budget_max/budget_min，元）与座位（passengers）解析；无锚定词的裸「N万」
    仅在预算语境（句含 预算/落地/价位 且紧跟其后的那一处）才视为预算。"""
    out: dict = {}
    m = BUDGET_RANGE_BOTH_RE.search(text)
    lo = None
    if m:
        out["budget_max"] = int(float(m.group(2)) * 10000)
        lo = float(m.group(1)) * 10000
    else:
        m = BUDGET_RANGE_RE.search(text)
        if m:
            out["budget_max"] = int(float(m.group(2)) * 10000)
            lo = float(m.group(1)) * 10000
        else:
            m = BUDGET_MAX_RE.search(text)
            if m:
                out["budget_max"] = int(float(m.group(1)) * 10000)
            else:
                m = BUDGET_MIN_RE.search(text)
                if m:
                    lo = float(m.group(1)) * 10000
                else:
                    m = BUDGET_MIN_LOOSE_RE.search(text)
                    if m:
                        lo = float(m.group(1)) * 10000
                    else:
                        # 裸「N万」：必须紧跟预算语境词（预算15万 ✓ / 销量30万 ✗）。
                        # 评审 R4#3：窗口放宽到 6 字（「预算在/预算大概15万」也算语境）
                        for m in BUDGET_BARE_RE.finditer(text):
                            if BUDGET_CONTEXT_RE.search(text[max(0, m.start() - 6):m.start()]):
                                out["budget_max"] = int(float(m.group(1)) * 10000)
                                break
    if lo is not None:
        out["budget_min"] = int(lo)
    seats = parse_passengers(text)
    if seats:
        out["passengers"] = seats
    return out

# 能源/车身关键词（流水线约束解析用；评测的措辞映射在 gen_eval_questions，判定共用）
_ENERGY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("纯电", "BEV"), ("插混", "PHEV"), ("能加油能充电", "PHEV"),
    ("增程", "EREV"), ("油电混动", "HEV"), ("混动", "HEV"), ("燃油", "ICE"), ("汽油", "ICE"),
)
_BODY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("SUV", "suv"), ("MpV", "mpv"), ("MPV", "mpv"), ("轿车", "sedan"), ("皮卡", "pickup"),
)
_NEGATION_CUE_RE = re.compile(r"(不要|不想|不买|不选|不看|排除|除了|别买)[^，。,；；]{0,2}$")


def _negated(text: str, start: int) -> bool:
    """关键词命中位置之前紧邻否定语（如「不要SUV」）→ 该处不构成约束。

    评审 R4#1：否定词不含单字「非」（「非常想要SUV」误伤）；间隙收紧到 2 字
    （「不要轿车要看SUV」的对比句不被跨句吞掉）。
    """
    return bool(_NEGATION_CUE_RE.search(text[max(0, start - 2):start]))


def parse_energy_body(text: str) -> dict:
    """能源/车身约束解析：多处命中且互斥时放弃（宁可少推不可推错）；否定语门控。"""
    out: dict = {}
    energies = {energy for kw, energy in _ENERGY_KEYWORDS if kw in text and not _negated(text, text.find(kw))}
    if len(energies) == 1:
        out["energy_type"] = next(iter(energies))
    # 「新能源」是独立约束键（非纯燃油即可，含插混/增程/油混），与具体能源类型并存
    if "新能源" in text and not _negated(text, text.find("新能源")):
        out["new_energy"] = True
    upper = text.upper()
    bodies = set()
    for kw, body in _BODY_KEYWORDS:
        pos = upper.find(kw.upper())
        if pos >= 0 and not _negated(text, pos):
            bodies.add(body)
    if len(bodies) == 1:
        out["body_type"] = next(iter(bodies))
    return out


# ── 车系属性装载（指纹化缓存）───────────────────────────────────────────────
_ATTR_CACHE: dict = {"fingerprint": None, "attrs": {}}


def _attrs_fingerprint(db) -> tuple:
    """与 series_index 的名称索引指纹同源，并纳入价格/事实表最大 id——
    评审 R4#8：min_price/max_seats 随价格与座位数据变化失效。
    pytest 下每个测试都是新建内存库、指纹可能碰撞，直接禁用缓存（同 _resolve_cache）。"""
    from app.catalog.series_index import _series_fingerprint

    if "PYTEST_CURRENT_TEST" in os.environ:
        return (os.environ["PYTEST_CURRENT_TEST"],)
    price_max = db.execute(select(func.max(OfficialPrice.id))).scalar()
    fact_max = db.execute(select(func.max(SpecFact.id))).scalar()
    return _series_fingerprint(db) + (price_max, fact_max)


def load_series_attrs(db, series_ids: set[int] | None = None) -> dict[int, dict]:
    """装载车系约束属性。series_ids=None 装全量；空集合返回空（评审 C12：不再静默全载）。"""
    if series_ids is not None and not series_ids:
        return {}
    fp = _attrs_fingerprint(db)
    if _ATTR_CACHE["fingerprint"] != fp:
        _ATTR_CACHE["fingerprint"] = fp
        _ATTR_CACHE["attrs"] = _load_all_attrs(db)
    if series_ids is None:
        return dict(_ATTR_CACHE["attrs"])
    return {sid: a for sid, a in _ATTR_CACHE["attrs"].items() if sid in set(series_ids)}


def _load_all_attrs(db) -> dict[int, dict]:
    """全量装载（指纹变化时一次）：在售款型、现行指导价、座位事实各查一次。"""
    variants = db.execute(
        select(VehicleVariant.id, VehicleVariant.series_id).where(VehicleVariant.status == "on_sale")
    ).all()
    prices: dict[int, float] = {}
    for p in db.execute(
        select(OfficialPrice.variant_id, OfficialPrice.price_cny).where(
            OfficialPrice.effective_to.is_(None)
        )
    ).all():
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

    variants_by_series: dict[int, list[int]] = {}
    for vid, sid in variants:
        variants_by_series.setdefault(sid, []).append(vid)
    out: dict[int, dict] = {}
    for s in db.scalars(select(VehicleSeries)).all():
        vids = variants_by_series.get(s.id, [])
        sv_seats = [seats[v] for v in vids if v in seats]
        out[s.id] = {
            "name": s.name,
            "brand_id": s.brand_id,
            "body_type": s.body_type,
            "energy_types": set(s.energy_types or []),
            "min_price": min((prices[v] for v in vids if v in prices), default=None),
            "max_seats": max(sv_seats) if sv_seats else None,
        }
    return out


# ── 参数问答键表 ────────────────────────────────────────────────────────────
# 键名与数据库 fact_key 对齐；phrase 为用户可读问法。
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

    支持的约束键：budget_max（元，车系在售最低指导价不超过）、budget_min（元，
    车系在售最低指导价不低于）、energy_type（BEV/PHEV/EREV/HEV/ICE，车系声明能源
    类型包含）、new_energy（非纯燃油）、body_type（suv/sedan/mpv/pickup）、
    passengers（最大座位数≥N）。
    """
    if not attr:
        return False
    budget = constraints.get("budget_max")
    if budget is not None and (attr["min_price"] is None or attr["min_price"] > budget):
        return False
    budget_min = constraints.get("budget_min")
    # 评审 R4#2：min_price 与 budget_max 同向——无在售价的车系不得因缺数据而
    # 空洞满足「20万以上」
    if budget_min is not None and (attr["min_price"] is None or attr["min_price"] < budget_min):
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
