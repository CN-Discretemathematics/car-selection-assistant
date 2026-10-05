"""车系名称索引与核心参数聚合（目录服务层）。

从消息文本解析真实车系（车系名/品牌+车系名/别名，归一化子串匹配），
并把在售 SKU 事实聚合为「核心参数」一句话画像。供 Agent 问答（series_qa）
与 RAG 流水线（app/rag：查询理解、车系摘要切片）共同复用，避免相互依赖。
"""
from __future__ import annotations

import os
import re
from collections import Counter
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

#: 核心参数那一行的前缀。车系问答卡片与 RAG 车系摘要切片**共用**——
#: 两个展示点对同一个车系说同一组数，措辞必须一致（用户 2026-10-05 拍板）。
#: N6-B 曾在 2026-10-05 短暂写成「（最高配）」，被实测证伪：`rank_headlines` 的口径
#: 是逐 label 极值，油耗/加速取 min（最省/最快）恰恰通常是低配。汉的
#: 「续航 705km（←3 款 EV）+ 油耗 0.67L（←5 款 DM-i 插混）」在库里根本不存在。
#: 常量放在本模块而不是各自的消费点，是为了**结构上**杜绝再次漂移。
HEADLINE_PREFIX = "核心参数（全系极值）："


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


#: 允许进对话解析索引的**单汉字**车系名。逐字裁决，理由见 `_load_name_entries` 注释。
#: 已知接受的误伤：放开「炮」后，「大炮」「炮灰」这类句子会命中长城炮（实测语料里罕见）。
_SINGLE_CHAR_SERIES_ALLOWED = frozenset({"汉", "炮"})


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
            #
            # 2026-10-05：`len(norm) >= 2` 是**为纯数字短名设的门槛**，却把
            # **单汉字车系名**也一起砍了。实测全库只有 3 个单字车系名
            # （比亚迪汉 20 款 / 比亚迪夏 4 款 / 长城炮 62 款），它们**全部**
            # 进不了索引，用户点名完全解析不出来：
            #   「汉怎么样」/「汉的续航多少」/「炮怎么样」 → 0 个车系
            # 中文没有词边界，单字命中必然带误伤，所以**逐字裁决**而不是一律放开：
            #   放开 汉：用户口语就说「汉」，且该字几乎不作独立常用词，误伤≈0。
            #        炮：长城炮 62 款，购车语境里「炮」基本只指它。
            #   挡住 夏：「夏天买车合适吗」实测会误判为比亚迪夏（现有重叠判定
            #        拦不住——「夏天」不是候选名），而比亚迪夏只有 4 款。
            # 新增单字车系名时**必须**在这里逐字裁决：先拿真实购车语料跑一遍
            # 「<该字><常见构词>」的句子（夏天 / 大炮 / 炮灰…），确认不误伤再加入。
            if (
                norm not in seen
                and not norm.isdigit()
                and (len(norm) >= 2 or norm in _SINGLE_CHAR_SERIES_ALLOWED)
            ):
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


def _first_free_span(
    msg: str, norm: str, chosen: list[tuple[int, int, int, str]]
) -> tuple[int, int] | None:
    """`norm` 在 `msg` 里**第一个不与已选跨度重叠**的出现位置；找不到返回 None。"""
    start = 0
    while True:
        pos = msg.find(norm, start)
        if pos < 0:
            return None
        end = pos + len(norm)
        if not any(pos < oend and end > ostart for ostart, oend, _, _ in chosen):
            return pos, end
        start = pos + 1


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
        # 2026-10-05：此前是 `start = msg.find(norm)`——**只看首次出现**。
        # 短名出现在**后面**时会被误杀：「汉L和汉怎么选」里「汉」在位置 4，
        # 但 find 返回 0（落在「汉l」的跨度 [0,2) 内），重叠判定把它当重叠跳过了，
        # 于是用户点名的「汉」被静默丢掉。改为**找第一个不被已选跨度覆盖的位置**。
        span = _first_free_span(msg, norm, chosen)
        if span is None:
            continue  # 全部出现位置都被更长的名字覆盖（腾势Z9 ⊂ 腾势Z9GT）
        start, end = span
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


#: 品牌名里可省略的通用后缀——「小米汽车」对用户就是「小米」，拼到车系名前会重复。
_GENERIC_BRAND_SUFFIXES = ("汽车", "集团", "公司", "科技", "控股")


def _brand_leads_series(brand_name: str, series_name: str) -> bool:
    """车系名是否**已经**带上了品牌标识，再拼一遍只会重复。

    原来的 `series_name.startswith(brand_name)` 只挡**完全**前缀，于是
    品牌「小米汽车」+ 车系「小米SU7」会拼出「**小米汽车小米SU7**」——真实库上
    有 9 个这样的车系（江淮 4 / 小米 3 / 吉利 2），其中小米SU7、SU7 Ultra、YU7
    是高曝光车系，用户第一眼就能看见这个重复。

    这里按「品牌名去掉通用后缀与纯英文词后，剩下的中文词是否已在车系名里」判断，
    于是「小米汽车」的核心词「小米」能认出「小米SU7」，而「特斯拉」认不出「Model Y」
    （仍拼成「特斯拉Model Y」）。

    判据是「**包含**」而不是「前缀」：品牌词未必在开头——「几何」+「吉利几何A」、
    「启源」+「长安启源A06」、「大众」+「一汽-大众CC」、「本田」+「东风本田S7」、
    「大通」+「上汽大通MAXUS H90房车」这 24 个车系，重复的词在中间。
    最初只按前缀判，漏掉了这 24 个——是反向验证的变异体把它逼出来的。

    全库实测：908 个车系里 **33 个**会因此改变（9 个重复在开头 + 24 个重复在中间），
    其余 875 个逐字不变。
    """
    if series_name.startswith(brand_name):
        return True
    stem = brand_name
    for suffix in _GENERIC_BRAND_SUFFIXES:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 2:
            stem = stem[: -len(suffix)]
    return any(
        # len>=2：单字词当判据太弱（真实库 92 个品牌里没有单字中文品牌，
        # 这条是防御性的，**没有测试覆盖**，改它时别当成有依据的行为）
        len(token) >= 2 and not token.isascii() and token in series_name
        for token in stem.split()
    )


def display_name(series: VehicleSeries, brand: Brand | None) -> str:
    if brand and brand.name and not _brand_leads_series(brand.name, series.name):
        return f"{brand.name}{series.name}"
    return series.name


def _numeric(text: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


_KEY_UNIT_RE = re.compile(r"\(([^()]*)\)[^()]*$")
_UNIT_CHARS = re.compile(r"^[A-Za-z0-9%·²³/°]+$")
#: 键尾括号（半角或全角，且必须在末尾）——「轴距(mm)」「电池快充时间（小时）」
_KEY_TAIL_PAREN_RE = re.compile(r"[（(]([^（）()]*)[)）]\s*$")
#: 键尾括号后跟**工况代号**的形态——「最低荷电状态油耗(L/100km)WLTC」。
#: 只认这四个已知的工况代号：库里还存在「全场景领航辅助(NOA)订阅￥320/月」这种
#: 括号后接散文的键，泛化的「括号 + 任意尾巴」会把散文也当单位剥掉。
_CYCLE_TOKENS = ("CLTC", "NEDC", "WLTC", "EUDC")
_KEY_UNIT_CYCLE_RE = re.compile(r"[（(]([^（）()]*)[)）](" + "|".join(_CYCLE_TOKENS) + r")\s*$")
#: 键尾括号里**确实是单位**的全集（大小写敏感，从全库 1365 个 fact_key 的
#: 括号内容实测归纳而来：60 种里 22 种是真单位）。
#:
#: **为什么不用「形状像单位」的规则**：`^[A-Za-z0-9%·²³/°]+$` 太宽松，库里这些
#: 缩写会全部被误判成单位并塞到值上——
#:   `全场景领航辅助(NOA)订阅￥320/月` → 值后面多一个「NOA」
#:   `全地形轮胎（AT）`              → 值后面多一个「AT」
#:   `便携式充电枪 (ICCB)`            → 「ICCB」是接口标准，不是单位
#:   `风阻系数(Cd)`                  → Cd 是无量纲系数，不是单位
#:   `长续航电池包(100kWh)`           → 「100kWh」是电池包规格，属于名字的一部分
#: 判据从「形状像」改成「确实在库里当单位用过」，才是可枚举、可审计的。
_KNOWN_UNITS = frozenset(
    {
        "mm", "L/100km", "kW", "L", "km", "kg", "N·m", "%", "°", "Ps", "V",
        "s", "m", "rpm", "kWh", "W", "mL", "km/h", "Ah", "Wh/kg", "kWh/100km",
        # 中文单位词
        "小时", "分钟", "秒", "英寸", "吋", "匹", "千瓦", "牛米", "个",
    }
)


def split_key_unit(key: str) -> tuple[str, str | None]:
    """键 → (去掉尾部单位括号后的名字, 单位)；不是单位括号则单位为 None。

    **唯一一处**的「键尾括号是不是单位」判定。2026-10-05 审查实锤：此前
    `unit_from_key`（半角+ASCII）与 `series_qa.display_fact_key`（全角+中文词表）
    是两个**各自独立**的识别器，于是：

        电池快充时间(小时)   标签剥掉 (小时)，但值侧 unit_from_key 认不出「小时」
        电池快充时间(分钟)   标签同样剥成「电池快充时间」

    同一回答里两个**量纲差 60 倍**的东西同名，且两边都没有单位可区分
    （实测 877 车系 × 10 组提问 32347 行里有 **719 行**如此）。
    两个识别器必须共用同一个判据，否则改一边就会漂。

    识别的两种形态（库里实测就这两种）：
      1. 括号在末尾：            轴距(mm) / 电池快充时间（小时）
      2. 括号 + 已知工况代号：    最低荷电状态油耗(L/100km)WLTC
         ——第 2 种是 `unit_from_key` 早就支持的（其 docstring 明确写了
         「键尾带工况文本也兼容」），**合并时若只认第 1 种就会把这批单位弄丢**，
         实测 `最低荷电状态油耗(L/100km)WLTC = 4.7（WLTC）` 少了 L/100km。

    单位判定用**显式白名单** `_KNOWN_UNITS`（从全库括号内容归纳），
    不用「形状像单位」的规则——库里 `NOA` / `AT` / `ICCB` / `Cd` / `100kWh`
    都能通过 ASCII 字符集，却都不是单位。

    括号里的中文名字一律保留（剥掉就丢真实信息）：
    `540°全景影像系统(带透明底盘)`、`L2级组合驾驶辅助包（限时免费）`、
    `便携式充电枪 (ICCB)`。
    """
    for pattern in (_KEY_UNIT_CYCLE_RE, _KEY_TAIL_PAREN_RE):
        matched = pattern.search(key)
        if not matched:
            continue
        inner = matched.group(1).strip()
        if inner in _KNOWN_UNITS:
            return key[: matched.start()].strip(), inner
        return key, None
    return key, None


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
#: 库里把多个取值塞进一个 fact_value 的分隔符（实测命中：电池 `29.165~74.96/75.26`）。
_MULTI_VALUE_RE = re.compile(r"[/、,，]")


def _split_embedded_unit(value: str) -> tuple[str, str]:
    """拆出值里自带的单位后缀（「150kW」→「150」「kW」）；拆不出则原样返回。

    尾缀必须落在 `_KNOWN_UNITS` 白名单里才认——否则「高配」这种以非数字结尾的
    值会被劈成「高~低 配」这种胡说八道（`_VALUE_UNIT_RE` 只是形状规则，不认白名单）。
    """
    matched = _VALUE_UNIT_RE.match(value.strip())
    if not matched or not matched.group(1).strip():
        return value.strip(), ""
    suffix = matched.group(2)
    if suffix not in _KNOWN_UNITS:
        return value.strip(), ""
    return matched.group(1), suffix


def _range_text(best: _Entry, group: list[_Entry]) -> str | None:
    """`best` 所在那一组（同 fact_key + 同单位 + 同测试口径）取值 >=2 档时的区间文本。

    返回 None 表示「合不出可信区间」，调用方退回单值——此时输出与改动前逐字一致。

    N9（2026-10-05 实测）：`rank_headlines` 逐 label 取极值，784 个多款型车系里
    **396 个（50.5%）**的核心参数整句没有任何一款型能复现——即
    「汉：续航 705km（来自 3 款 EV）；油耗 0.67L（来自 5 款 DM-i 插混）」，
    这台车在库里根本不存在。单值同样会骗人（小米SU7「最高配续航 902km」实际来自
    中配后驱Pro，顶配四驱Max 为性能牺牲了续航）。改成区间后，展示的每个数都是
    **真实存在过的值**，不会再造出虚构配置。

    **影响面（2026-10-05 独立复核订正）**：784 个多款型车系里 **610 个（77.8%）**
    输出会变，只有 174 个逐字不变。此前这里写的是「388/784 个车系零影响」——
    388 是「未被拼接」的车系数，而**未被拼接 ≠ 文本不变**：星愿 6 款、未被拼接，
    但动力 85→58~85、续航 480→310~480，照样出区间。影响面比当初估的 2.2 倍。

    **合区间的条件是「同一个量」**——(fact_key, unit, cycle) 三元组全等才算同一档：
      - **fact_key**（本次新增、真正起作用的一层）：`CLTC综合续航` 与
        `CLTC纯电续航里程` 都是 km+CLTC 却是两个量，合成 `125~705 km` 会让用户
        以为纯电续航能到 705。首个版本漏了这层，被自己的测试抓出来；
      - **unit**：这一层其实是**冗余的**——`best` 恒取自 `same_unit` 组，组内 unit
        已经全等（上游 `ref_unit` 分组在改动前就挡住了跨单位）。保留在三元组里是
        防将来重构时把 `group` 换成别的集合；
      - **cycle**：WLTC 与 CLTC 的续航不能写进同一个区间。

    取「极值所在的那一组」而不是全部：极值来自哪个键/单位/口径，区间就只在该组内取。
    实测「汉」的 705 落在 `CLTC纯电续航里程(km)`（该车系 40 行续航事实全在这一键，
    `CLTC综合续航` 一行都没有），于是输出 `125~705 km（CLTC）`——DM-i 插混的 125
    与纯电的 705 都在库里真实存在，这正是改前那个孤零零的 `705` 藏掉的信息。
    """
    cohort = [
        e
        for e in group
        if (e.key, e.unit, e.cycle) == (best.key, best.unit, best.cycle)
    ]
    numeric = [e for e in cohort if e.num is not None]
    if len({e.num for e in numeric}) < 2:
        return None
    lo = min(numeric, key=lambda e: e.num)  # type: ignore[type-var]
    hi = max(numeric, key=lambda e: e.num)  # type: ignore[type-var]
    # 库里存在「多值串」脏数据（实测 1671 个区间 cell 命中 5 个，如 AION i60 的
    # 电池 `29.165~74.96/75.26 kWh`）：端点是整串，挑不出到底取哪个当上下界，
    # 拼出来的新格式反而比旧单值更难读（高端口会被读成 `74.96/75.26`）。退回单值。
    if _MULTI_VALUE_RE.search(lo.raw) or _MULTI_VALUE_RE.search(hi.raw):
        return None
    suffix = f"（{best.cycle}）" if best.cycle else ""
    if best.unit:
        return f"{lo.raw.strip()}~{hi.raw.strip()} {best.unit}{suffix}"
    # 单位写在值里（如「150kW」）：两端要拆出同一个后缀才拼，否则原样并列
    lo_body, lo_unit = _split_embedded_unit(lo.raw)
    hi_body, hi_unit = _split_embedded_unit(hi.raw)
    if lo_unit and lo_unit == hi_unit:
        return f"{lo_body}~{hi_body}{lo_unit}{suffix}"
    return f"{lo.raw.strip()}~{hi.raw.strip()}{suffix}"


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
            # 注意 `unit_matched` **恒非空**（`numeric[0].unit` 按定义就等于 ref_unit），
            # 所以它不是一道独立的闸门，只是「取哪个单位组参与比较」；单位一致性
            # 真正由 `_range_text` 的 cohort 三元组（key, unit, cycle）保证。
            ref_unit = numeric[0].unit
            same_unit = [e for e in numeric if e.unit == ref_unit] or numeric
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
            out[label] = _range_text(best, same_unit) or best.text
        out_map[sid] = out
    return out_map


#: 尺寸的事实键（text 模式，见 `HEADLINE_SPECS`）
_SIZE_FACT_KEY = "长*宽*高(mm)"


def size_lines(
    db: Session, series_list: list[VehicleSeries]
) -> dict[int, str]:
    """批量算「尺寸」展示文本：{series_id: 文本}。单档照旧；多档取**在售款型里的众数**并标覆盖率。

    为什么单独查、不并进 `rank_headlines`：
      1. 覆盖率必须**按款型**去重——`SpecFact` 对 `(variant_id, fact_key)` 无唯一约束
         （全库 36241 组重复），按事实行累加会虚高。N1 已经吃过一次这个亏。
         而 `rank_headlines` 拿到的行里没有 `variant_id`。
      2. 尺寸是 text 模式「取首值」，那个首值取决于数据库返回顺序，本质是**任取**。
         实测 312/877（35.6%）的车系尺寸多档，其中 113 个（36.2%）众数与首值不同——
         改完这 113 个会真的换一个更有代表性的值。

    **批量而非逐车系查**（2026-10-05 审查 P2）：逐个查会让 3 车系对比多打 6 条 SQL，
    与「批量核心参数（此前逐车系单查，N 次查询）」的既有评审结论相悖。
    卡片与 RAG 切片都走这里——**两处口径必须同源**，否则切片（LLM 直接读）与卡片
    会为同一个车系报两个尺寸（实测 113 个车系打架，审查 P1-2）。

    并列第一时（实测 79 个车系）取 `(variant_id, fact_id)` 最小者——**SQL 必须显式
    `ORDER BY`**，SQLite 靠 rowid 恰好稳定，PostgreSQL 无此保证（审查 P2）。

    `rank_headlines` 侧的尺寸行为**不动**（仍取首值、不参与区间），
    本函数只在渲染时覆盖它。
    """
    if not series_list:
        return {}
    key_unit = unit_from_key(_SIZE_FACT_KEY) or ""
    rows = db.execute(
        select(
            VehicleVariant.series_id, VehicleVariant.id, SpecFact.fact_value,
            SpecFact.unit, SpecFact.id,
        )
        .join(SpecFact, SpecFact.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.series_id.in_([s.id for s in series_list]),
            VehicleVariant.status == "on_sale",
            SpecFact.fact_key == _SIZE_FACT_KEY,
        )
        # 并列取「最早」需要确定性：没有 ORDER BY 时 SQLite 靠 rowid 稳定，
        # PostgreSQL 不保证（审查 P2）。
        .order_by(VehicleVariant.series_id, VehicleVariant.id, SpecFact.id)
    ).all()

    # 同一车系内：{series_id: {variant_id: 首个尺寸文本}}
    by_series: dict[int, dict[int, str]] = {}
    for series_id, variant_id, value, row_unit, _fact_id in rows:
        if not value:
            continue
        # 行上的 unit 优先（与 `rank_headlines` 同口径），没有才从键名推
        unit = (row_unit or "").strip() or key_unit
        text = str(value).strip()
        if unit and text.lower().endswith(unit.lower()):
            text = text[: -len(unit)].strip()  # 值自带单位，别再拼一遍
        # 乘号写法混用（真实库 7 个车系同尺寸有 `*` 与 `×` 两种写法）会被当成两档，
        # 覆盖率随之失真（审查 P2）——归一后再入桶。
        text = text.replace("×", "*").replace("＊", "*")
        by_series.setdefault(int(series_id), {}).setdefault(
            int(variant_id), f"{text} {unit}".strip()
        )

    out: dict[int, str] = {}
    for series_id, per_variant in by_series.items():
        if not per_variant:
            continue
        counter = Counter(per_variant.values())
        # Counter 保插入序，most_common 在同票时保留「先出现」的那个（见 docstring）
        top_text, top_count = counter.most_common(1)[0]
        total = len(per_variant)
        # 单档：top_text 已含单位，不要再拼一遍（否则「mm mm」）
        out[series_id] = (
            top_text if len(counter) < 2
            else f"{top_text}（在售 {total} 款中 {top_count} 款为此尺寸）"
        )
    return out


def size_line(db: Session, series: VehicleSeries) -> str | None:
    """`size_lines` 的单车系版本（卡片路径用）。"""
    return size_lines(db, [series]).get(series.id)


def head_with_size(size: str | None, head: dict[str, str]) -> dict[str, str]:
    """把 `rank_headlines` 的尺寸项换成 `size_line` 的口径（众数 + 覆盖率）。

    四个展示点（单车系块、对比块、逐项对比行、RAG 车系摘要切片）都必须过这一道——
    少一个就会在同一条回答里出现两个互相矛盾的尺寸，或让 LLM 读到与卡片不同的数
    （2026-10-05 审查 P1-2：卡片走众数、切片仍走首值，实测 113 个车系打架）。
    `size` 由调用方从 `size_lines` 的**批量**结果里取，避免 N+1 查询（审查 P2）。
    """
    return {**head, "尺寸": size} if size else head


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
