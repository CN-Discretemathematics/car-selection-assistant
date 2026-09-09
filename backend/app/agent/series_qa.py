"""具体车系问答（「X 有什么优点」「X 和 Y 相比怎么选」）。

用户提到具体车系名（含车系名/品牌+车系名/别名），直接基于数据库真实参数回答：
- 单个车系：定位 / 官方指导价 / 核心参数 / 亮点配置 / 月销量；
- 多个车系：逐项对比主要参数与差异，并给出「是否同级别」的客观判断。

车系名解析与核心参数聚合在 app/catalog/series_index.py（RAG 流水线同源复用）。
原则 3/17：所有参数来自 SpecFact/官方指导价/销量表，绝不编造动态驾驶感受、
车主口碑、优惠信息；缺失字段如实标注「官方资料未披露」。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import services as catalog
from app.catalog.series_index import (
    HEADLINE_ORDER,
    unit_from_key,
    display_name,
    rank_headlines,
    series_fact_rows,
    series_headline,
)
from app.common.enums import MISSING_VALUE_LABEL
from app.common.models import Brand, SpecFact, VehicleSeries, VehicleVariant
from app.variants.normalization import display_fact_value, fact_display_label, fact_identity

_BODY_LABEL = {"sedan": "轿车", "suv": "SUV", "mpv": "MPV", "pickup": "皮卡"}
_ENERGY_LABEL = {"BEV": "纯电", "PHEV": "插混", "EREV": "增程", "HEV": "油混", "ICE": "燃油"}

# 车系提问触发器（单车系时还需命中才走车系问答；双车系以上无条件回答）
# 评审 M-R10：补参数提问句式——此前「Z9GT 续航多少/有没有冰箱」不命中，
# 落入追问/闲聊路径（数据库明明有参数却答非所问/说没有）。
# 补充（M-R11）：价格/指导价/配置/参数 同样直连数据库确定性答案——
# 此前「RAV4荣放官方指导价」不命中，闲聊路径只召回基础信息证据，LLM 会答「无可靠数据」。
_SERIES_QA_RE = re.compile(
    r"(优点|优势|亮点|卖点|缺点|怎么样|好不好|值得|值不值|性价比|推荐吗|对比|相比|比较|"
    r"区别|差别|哪个好|怎么选|选哪个|选哪款|选什么|介绍|了解|讲下|说下|看看|"
    r"想买|准备买|打算买|就要|关注|看中|"
    r"多少|多少钱|多大|多长|几升|几款|有没有|有没|带不带|带吗|配不配|配备|配了|支持吗|支持不|"
    r"价格|指导价|配置|参数|"
    r"是不是|耗油|油耗|电耗|续航|轴距|功率|马力|扭矩|加速|快充|慢充|电池|空间|"
    r"气囊|悬架|悬挂|底盘|隔音|座椅|天窗|屏幕|音响|智驾|辅助驾驶|自动驾驶|车机|芯片|"
    r"雷达|摄像头|抬头显示|冰箱|轮胎|轮毂|四驱|两驱|变速箱|排量|油箱|后备箱|行李厢|"
    r"离地间隙|风阻|质保|保修)"
)

# 参数探针（评审 M-R10）：提问关键词 → 事实键匹配。命中维度后从**全量 DB 事实**
# 直接取值并入回答（绕过检索索引的每车系采样上限），「DB 有参数却说没有」的根治。
_PARAM_PROBES: tuple[tuple[str, str], ...] = (
    # (提问关键词 regex, 事实键 regex)
    (r"(续航|能跑多少|跑多远)", r"(续航)"),
    (r"(油耗|电耗|能耗|耗油|费油|省电)", r"(油耗|电耗|耗电量|能耗)"),
    (r"(空间|轴距|车长|尺寸|后备箱|行李厢)", r"(长\*宽\*高|长度\(mm\)|宽度\(mm\)|高度\(mm\)|轴距|后备箱|行李厢|容积)"),
    (r"(动力|功率|马力|扭矩|加速|零百|几秒|推背)", r"(功率|马力|扭矩|0-100|加速|最高车速)"),
    (r"(电池|充电|快充|慢充)", r"(电池|快充|慢充|充电)"),
    (r"(安全|气囊|主动刹车|碰撞)", r"(气囊|主动安全|主动刹车|车道保持|并线辅助|碰撞)"),
    (r"(悬架|悬挂|底盘|四驱|越野|操控)", r"(悬架|悬挂|驱动|四驱|差速|底盘)"),
    (r"(智驾|辅助驾驶|自动驾驶|车机|芯片|屏幕|音响|抬头显示|雷达|摄像头|泊车)",
     r"(辅助驾驶|自动驾驶|巡航|芯片|屏幕|音响|扬声器|抬头显示|雷达|摄像头|泊车|车载智能)"),
    (r"(座椅|空调|天窗|冰箱|隔音|按摩|通风|加热|彩电|沙发)", r"(座椅|空调|天窗|冰箱|隔音|按摩|通风|加热)"),
)

# 探针回答里跳过的事实键/无信息量值（与 RAG 切片同一口径）
_PROBE_SKIP_KEYS = {"优惠信息"}
_PROBE_SKIP_VALUES = {"暂无", "-", "--", "未知"}
_PROBE_MAX_KEYS = 8
# 汽车之家配置表的特征标记值 → 用户可读表述（●=标配、○=选装；- 已在跳过表内）
_FEATURE_VALUE_LABEL = {"●": "有（标配）", "○": "选装"}

# 否定语境（评审 P2）：「我不买汉兰达」——被否定的车系不做问答、不加会话锁定
NEGATION_WORDS = ("不买", "不想买", "不想要", "不喜欢", "不考虑", "排除", "不要")
NEGATION_RE = re.compile("(" + "|".join(NEGATION_WORDS) + ")")
# 否定词与车系名之间允许的最大间隔字符数（不含标点）：超过就认为否定的是别的东西
_NEGATION_SERIES_GAP = 4
_NEGATION_SERIES_CACHE: dict[str, re.Pattern] = {}


def negates_series(message: str, series_name: str) -> bool:
    """否定词是否**直接指向**该车系（近距离共现），用于把「否定锁定车系」当成明确解锁。

    「我不买星愿了，想要15万的燃油车」→ True：锁定的星愿被否定，继续锁定必然推荐为空。
    「不想要SUV了，看看银河星愿」→ False：否定的是车身形式，星愿仍是用户想看的目标，
    此时解锁会把用户刚点名的车系丢掉（评审 m2 的修法必须区分这两种语境）。
    """
    if not series_name or not NEGATION_RE.search(message):
        return False
    pattern = _NEGATION_SERIES_CACHE.get(series_name)
    if pattern is None:
        words = "|".join(re.escape(w) for w in NEGATION_WORDS)
        pattern = re.compile(
            rf"(?:{words})[^，。,；;！!？?\n]{{0,{_NEGATION_SERIES_GAP}}}{re.escape(series_name)}"
        )
        _NEGATION_SERIES_CACHE[series_name] = pattern
    return bool(pattern.search(message))

# 亮点配置（用户可感知的进阶项；按顺序最多取 5 个实际存在的）
_FEATURE_HIGHLIGHTS: list[tuple[str, str]] = [
    ("空气悬架", "空气悬架"),
    ("魔毯智能悬架", "魔毯悬挂"),
    ("整体主动转向系统", "后轮转向"),
    ("坦克转弯", "坦克转弯"),
    ("高压快充", "高压快充"),
    ("对外放电", "对外放电"),
    ("热泵空调", "热泵空调"),
    ("车载冰箱", "车载冰箱"),
    ("AR-HUD增强现实抬头显示", "AR-HUD"),
    ("透明底盘/540度影像", "透明底盘"),
    ("记忆泊车", "记忆泊车"),
    ("遥控泊车", "遥控泊车"),
    ("前排中间气囊", "前排中间气囊"),
    ("主动刹车/主动安全系统", "主动安全"),
    ("车道保持辅助系统", "车道保持"),
    ("自动开合车门", "自动开关门"),
    ("后排独立空调", "后排独立空调"),
    ("第二排座椅电动调节", "二排电动调节"),
]


def probe_facts(
    facts: list[tuple[str, str, str | None, str | None]],
    message: str,
) -> list[str]:
    """按需参数查找（评审 M-R10）：提问命中的维度 → 「键 = 值」文本行。

    数据来自该车系**全量**在售事实（series_fact_rows），不受 RAG 索引采样限制；
    同键跨款多值时最多展示两个并标注差异，无信息量值（暂无/优惠信息）跳过。
    """
    matched_keys: list[str] = []
    for query_re, key_re in _PARAM_PROBES:
        hit = re.search(query_re, message)
        if not hit:
            continue
        keyword = hit.group(0)
        compiled = re.compile(key_re)
        # 先收「提问原词直接命中」的键（如问「冰箱」→ 车载冰箱 必在首位），
        # 再按维度补齐——否则宽维度键按事实表顺序填满名额，问的具体项被截掉
        for key, _v, _u, _c in facts:
            if keyword in key and key not in matched_keys and key not in _PROBE_SKIP_KEYS:
                matched_keys.append(key)
        for key, _v, _u, _c in facts:
            if compiled.search(key) and key not in matched_keys and key not in _PROBE_SKIP_KEYS:
                matched_keys.append(key)
        if len(matched_keys) >= _PROBE_MAX_KEYS:
            break

    values_by_key: dict[str, list[tuple[str, str | None, str | None]]] = {}
    for key, value, unit, cycle in facts:
        if key not in matched_keys:
            continue
        if not value or value.strip() in _PROBE_SKIP_VALUES:
            continue
        entries = values_by_key.setdefault(key, [])
        entry = (value.strip(), unit, cycle)
        if entry not in entries:
            entries.append(entry)

    lines: list[str] = []
    for key in matched_keys[: _PROBE_MAX_KEYS]:
        entries = values_by_key.get(key) or []
        if not entries:
            continue
        rendered: list[str] = []
        for value, unit, cycle in entries[:2]:
            value = _FEATURE_VALUE_LABEL.get(value, value)  # ● → 有（标配）、○ → 选装
            unit = unit or unit_from_key(key) or ""
            if unit and value.lower().endswith(unit.lower()):
                unit = ""
            rendered.append(f"{value}{f' {unit}' if unit else ''}{f'（{cycle}）' if cycle else ''}")
        suffix = "（不同款型存在差异）" if len(entries) > 1 else ""
        lines.append(f"{key} = {' / '.join(rendered)}{suffix}")
    return lines


def should_answer(resolved: list[tuple[VehicleSeries, Brand | None]], message: str) -> bool:
    """是否走「车系问答」而不是推荐/追问链路：
    - 命中 2 个及以上车系 → 用户在做车型对比，直接回答；
    - 命中 1 个车系 + 提问触发词（优点/怎么样/想买…）→ 回答；
    - 否定语境（「我不买汉兰达」）→ 不做问答，交给推荐链路处理排除。
    """
    if len(resolved) >= 2:
        return True
    if not resolved:
        return False
    if NEGATION_RE.search(message):
        return False
    return bool(_SERIES_QA_RE.search(message))


def series_highlights(db: Session, series: VehicleSeries) -> list[str]:
    """车系实际配备的亮点配置（最多 5 项）。"""
    present = set(
        db.execute(
            select(SpecFact.fact_key)
            .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
            .where(VehicleVariant.series_id == series.id, VehicleVariant.status == "on_sale")
            .distinct()
        ).scalars()
    )
    return [label for key, label in _FEATURE_HIGHLIGHTS if key in present][:5]


def _price_text(db: Session, series: VehicleSeries) -> str:
    low, high = catalog.series_price_range(db, series.id)
    if low is None:
        return "官方资料未披露"
    if high is not None and high != low:
        return f"{low / 10000:g}-{high / 10000:g} 万元"
    return f"{low / 10000:g} 万元"


def _sales_text(db: Session, series: VehicleSeries) -> str:
    sales = catalog.latest_sales(db, series.id)
    if sales is None or sales.sales_count is None:
        return ""
    label = "门户口径" if sales.sales_type == "portal" else "零售口径"
    return f"；{sales.month} 月销量 {sales.sales_count:,} 辆（{label}）"


def _series_header(db: Session, series: VehicleSeries, brand: Brand | None) -> str:
    return (
        f"{series.positioning or '定位未标注'} · "
        f"{_BODY_LABEL.get(series.body_type or '', series.body_type or '车身未标注')} · "
        f"{' / '.join(_ENERGY_LABEL.get(t, t) for t in (series.energy_types or [])) or '能源未标注'} · "
        f"官方指导价 {_price_text(db, series)}"
    )


def _describe(db: Session, series: VehicleSeries, brand: Brand | None) -> str:
    name = display_name(series, brand)
    parts = [f"「{name}」：{_series_header(db, series, brand)}"]
    head = series_headline(db, series)
    if head:
        order = [label for label in HEADLINE_ORDER if label in head]
        parts.append("核心参数：" + "；".join(f"{label} {head[label]}" for label in order))
    highlights = series_highlights(db, series)
    if highlights:
        parts.append("亮点配置：" + "、".join(highlights))
    sales = _sales_text(db, series)
    if sales:
        parts.append("市场表现：" + sales.lstrip("；"))
    return "\n".join(parts)


def build_series_qa_answer(
    db: Session,
    resolved: list[tuple[VehicleSeries, Brand | None]],
    message: str,
) -> str:
    """生成车系问答的确定性回答文本（所有内容来自数据库事实）。"""
    footer = "以上基于汽车之家参数配置页与官方指导价整理（动态驾驶感受、车主口碑与优惠信息不在数据范围内），具体以品牌官网为准。"

    if len(resolved) == 1:
        series, brand = resolved[0]
        name = display_name(series, brand)
        parts = [f"关于「{name}」：\n" + _describe(db, series, brand)]
        # 按需参数查找（评审 M-R10）：提问命中的维度从全量 DB 事实直接取值
        facts = series_fact_rows(db, [series.id]).get(series.id, [])
        probed = probe_facts(facts, message)
        if probed:
            parts.append("你问到的相关参数：" + "；".join(probed) + "。")
        parts.append(footer)
        return "\n".join(parts)

    # 批量核心参数（评审 P2：此前多车系对比逐车系单查，N 次查询）
    facts_by_series = series_fact_rows(db, [s.id for s, _ in resolved])
    heads_map = rank_headlines(facts_by_series)

    blocks: list[str] = ["你说的这两款车我先放在一起看："]
    for series, brand in resolved:
        name = display_name(series, brand)
        blocks.append(f"\n【{name}】{_series_header(db, series, brand)}")
        head = heads_map.get(series.id, {})
        if head:
            order = [label for label in HEADLINE_ORDER if label in head]
            blocks.append("  核心参数：" + "；".join(f"{label} {head[label]}" for label in order))
        # 按需参数查找（评审 M-R10）：对比语境下同样回答问到的具体参数
        probed = probe_facts(facts_by_series.get(series.id, []), message)
        if probed:
            blocks.append("  你问到的相关参数：" + "；".join(probed))
        highlights = series_highlights(db, series)
        if highlights:
            blocks.append("  亮点配置：" + "、".join(highlights))
        sales = _sales_text(db, series)
        if sales:
            blocks.append(f"  {sales.lstrip('；')}")

    # 逐项对比（双方都有数据的量纲）
    heads = [heads_map.get(series.id, {}) for series, _ in resolved]
    diff: list[str] = []
    for label in HEADLINE_ORDER:
        values = [h.get(label) for h in heads]
        if all(values):
            diff.append(f"{label}：{values[0]} vs {values[1]}")
    if diff:
        blocks.append("\n同量纲参数对比：" + "；".join(diff))

    # 客观小结（同级/异级判断，不含主观推荐）
    first, second = resolved[0][0], resolved[1][0]
    same_class = first.positioning and first.positioning == second.positioning
    if same_class:
        blocks.append(
            "小结：两款车同属「" + first.positioning + "」级别，但价格与动力总成差异决定了买点不同；"
            "可以按用车场景（通勤/家庭/长途）和预算取舍，或到品牌官网查看具体款型配置表。"
        )
    else:
        blocks.append(
            "小结：两款车级别与价格区间差异明显，直接比「谁更好」意义不大；"
            "更合适的做法是按预算与用途缩小范围——告诉我预算和主要用途，我可以帮你筛真正同档的候选。"
        )
    blocks.append("\n" + footer)
    return "\n".join(blocks)


# ── 单车系「版本 / 款型差异」问答 ─────────────────────────────────────────────
# 用户反馈（评审 P1）：「星愿不同版本有什么区别」此前落到单车系档案路径——
# series_fact_rows 是**车系级聚合**（同键跨款归并成一行），版本之间的差异被抹平，
# 用户看不到「310km 版 vs 410km 版」的区别，甚至被答成「没有数据」。
# 这里补一条确定性路径：直接按在售 SKU 逐款列出指导价与**只列不同项**的差异
# （与对比模块「隐藏相同参数」同口径），数据缺失如实标注，绝不猜测。
_VARIANT_HINT_RE = re.compile(
    r"(版本|款型|款式|配置版本|各款|各版本|不同款|不同版本|哪款|哪个版本|哪个配置|"
    r"顶配|次顶配|低配|高配|中配|入门版|旗舰版)"
)
_VARIANT_ASK_RE = re.compile(
    r"(区别|差别|差异|不同|不一样|对比|比较|哪个好|哪款好|怎么选|选哪|差在哪|差多少|贵在哪|贵多少|值不值|值得吗)"
)
_VARIANT_DIFF_MAX = 6        # 文本内最多列出的版本数（超出按指导价取前 N 并提示）
_VARIANT_DIFF_KEYS_MAX = 12  # 差异项最多列几条（其余引导用「加入对比」看全表）
_VARIANT_DIFF_SKIP_KEYS = {"优惠信息"}
# 价格类事实键跳过：每个版本的官方指导价已在版本清单里单独列出，若再作为「差异项」
# 重复一行，真实数据里会出现「厂商指导价(元)：6.48 元」这类单位失真的冗余行。
_VARIANT_DIFF_SKIP_KEY_WORDS = ("指导价", "报价", "价格", "优惠")
# 复合值（尺寸「4135*1805*1570」、轮胎「225/45 R18」、座椅「主●/副-」）不做数值归一化：
# 归一化会把分隔符后半段误当单位（→「4135 mm」）或凭空插入空格（→「4135 *1805*1570」）。
_COMPOSITE_VALUE_RE = re.compile(r"[*×/、,，]")
# 差异项展示优先级：用户区分版本时最关心的维度在前
_VARIANT_DIFF_PRIORITY: tuple[str, ...] = (
    "续航", "电池能量", "电动机总功率", "系统综合功率", "最大功率", "最大扭矩", "加速",
    "油耗", "电耗", "快充", "座位数", "驱动", "轮毂", "轮胎",
    "空气悬架", "辅助驾驶", "自动驾驶", "巡航", "座椅", "天窗", "音响", "扬声器",
    "抬头显示", "车载冰箱", "泊车",
)
_MARKS = "①②③④⑤⑥⑦⑧⑨⑩"


def asks_variant_diff(message: str) -> bool:
    """是否在问「同一车系不同版本 / 款型的差异」。"""
    return bool(_VARIANT_HINT_RE.search(message) and _VARIANT_ASK_RE.search(message))


def _variant_value(fact: SpecFact) -> str:
    """单条事实的展示值（与对比模块同口径；配置表标记值转可读文本）。"""
    raw = (fact.fact_value or "").strip()
    if not raw or raw in _PROBE_SKIP_VALUES:
        return MISSING_VALUE_LABEL
    if raw in _FEATURE_VALUE_LABEL:
        return _FEATURE_VALUE_LABEL[raw]
    unit = (fact.unit or "").strip()
    if _COMPOSITE_VALUE_RE.search(raw):
        parts = [raw]
        if unit and not raw.endswith(unit):
            parts.append(unit)
        if fact.cycle:
            parts.append(f"({fact.cycle})")
        return " ".join(parts)
    display = display_fact_value(raw, fact.unit, fact.cycle) or MISSING_VALUE_LABEL
    # 无单位且原值不含空格时回到原值，避免归一化插入多余空格
    if not unit and " " not in raw and display.replace(" ", "") == raw:
        display = raw + (f" ({fact.cycle})" if fact.cycle else "")
    return display


def _diff_rank(key: tuple[str, str], label: str) -> tuple[int, int, str]:
    """差异项排序：命中优先级关键词的在前，其余按分类+键名稳定排序。"""
    fact_key = key[1]
    for idx, kw in enumerate(_VARIANT_DIFF_PRIORITY):
        if kw in label or kw in fact_key:
            return (0, idx, fact_key)
    return (1, 0, key[0] + fact_key)


def build_variant_diff_answer(
    db: Session, series: VehicleSeries, brand: Brand | None, message: str = ""
) -> tuple[str, list[dict]]:
    """同一车系内「版本差异」确定性回答。

    返回 (文本, 版本行)。版本行供前端渲染候选卡片与「加入对比」动作。
    事实全部来自在售 SKU 的 SpecFact / OfficialPrice；无数据时如实说明并给出官网入口。
    """
    name = display_name(series, brand)
    rows: list[tuple[VehicleVariant, float | None]] = []
    for variant in catalog.series_variants(db, series.id, on_sale_only=True):
        price = catalog.variant_current_price(db, variant.id)
        rows.append((variant, float(price.price_cny) if price else None))
    rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0.0))

    archived = False  # 无在售款型时降级展示库内归档款型（标注停售）
    if not rows:
        # 2026-09 部署实测：部分车系（护卫舰07/宝骏云朵/凯美瑞旧代次等 31 个）款型
        # 全为停售——数据本身完整，直接展示并标注停售，比「未收录」死胡同更有用
        for variant in catalog.series_variants(db, series.id, on_sale_only=False):
            price = catalog.variant_current_price(db, variant.id)
            rows.append((variant, float(price.price_cny) if price else None))
        rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0.0))
        archived = True
        if not rows:  # 连款型都没有（极少数）：保留原说明
            official = f"：{series.official_page_url}" if series.official_page_url else "。"
            text = (
                f"【{name}】库内暂未收录该车型的款型数据。"
                f"可能原因：数据源尚未收录。可先到品牌官网查看配置表{official}\n"
                + "以上口径：只陈列数据库既有事实，绝不编造。"
            )
            return text, []

    footer = (
        "以上为数据库在售 SKU 的官方指导价与配置事实；标注「"
        + MISSING_VALUE_LABEL
        + "」表示暂未收录，不代表没有该配置。可在下方候选中点「加入对比」查看完整参数表。"
    )
    if archived:
        footer = (
            f"注意：该车系当前**无在售款型**（库内 {len(rows)} 个款型均为停售/未标注在售），"
            "以上为已归档数据，仅供参考；如需在售车型可告诉我预算与用途，我帮你找同类替代。"
        )

    shown = rows[:_VARIANT_DIFF_MAX]
    if archived:
        head = f"【{name}】库内归档 {len(rows)} 个款型（均已停售，仅供参考）"
    else:
        head = f"【{name}】在售 {len(rows)} 个版本"
    if len(rows) > len(shown):
        head += f"（按官方指导价从低到高列出前 {len(shown)} 个）"
    lines = [head + "："]
    for idx, (variant, price) in enumerate(shown):
        label = variant.config_version or variant.display_name
        price_text = f"{price / 10000:g} 万元" if price is not None else MISSING_VALUE_LABEL
        lines.append(f"{_MARKS[idx]} {label}：官方指导价 {price_text}")

    # 逐款事实 → (分类, 键) 归并；用归一化身份判断「相同 / 不同」（与对比模块一致）
    keys: dict[tuple[str, str], dict[int, tuple[tuple, str]]] = {}
    labels: dict[tuple[str, str], str] = {}
    for variant, _ in shown:
        for fact in catalog.variant_facts(db, variant.id):
            if fact.fact_key in _VARIANT_DIFF_SKIP_KEYS:
                continue
            if any(word in fact.fact_key for word in _VARIANT_DIFF_SKIP_KEY_WORDS):
                continue
            key = (fact.category, fact.fact_key)
            keys.setdefault(key, {})[variant.id] = (
                fact_identity(fact.category, fact.fact_key, fact.fact_value or "", fact.unit, fact.cycle),
                _variant_value(fact),
            )
            labels.setdefault(key, fact_display_label(fact.fact_key, fact.unit, fact.cycle))

    def _vector(key: tuple[str, str]) -> tuple[str, ...]:
        """某事实键在各版本上的展示值向量（缺收录的版本记为未披露）。"""
        return tuple(keys[key].get(v.id, ((), MISSING_VALUE_LABEL))[1] for v, _ in shown)

    # 差异项 = ①归一化后各版本取值不全相同的键，或 ②只有部分版本收录到该事实的键；
    # 再按「标签」与「取值向量」去重：同一维度常有多个近义键（电动机总功率 /
    # 后电动机最大功率 / 最大功率，快充时间(分钟) / (小时)），全列出来会挤掉真正有
    # 区分度的差异项。
    # 评审 M2：②必须算差异。汽车之家对未配备的款型填「-」，导入层按占位值跳过
    # （autohome_sku.SKIP_VALUES），于是「顶配独有空气悬架 / 车载冰箱」在库里只剩
    # 1 行事实——旧口径只比较「已有该键的款型」，会把这类最常见的版本差异整片漏掉。
    # 缺失侧统一渲染 MISSING_VALUE_LABEL，页脚已声明「未收录 ≠ 没有该配置」。
    diff_keys = [
        k for k, m in keys.items()
        if len({ident for ident, _ in m.values()}) > 1 or len(m) < len(shown)
    ]
    # 同优先级内，「所有版本都收录到、但取值不同」的键排在「部分版本缺收录」之前
    diff_keys.sort(key=lambda k: (_diff_rank(k, labels[k]), 0 if len(keys[k]) == len(shown) else 1))
    picked: list[tuple[str, str]] = []
    seen_labels: set[str] = set()
    seen_vectors: set[tuple[str, ...]] = set()
    for key in diff_keys:
        label, vector = labels[key], _vector(key)
        if label in seen_labels or vector in seen_vectors:
            continue
        seen_labels.add(label)
        seen_vectors.add(vector)
        picked.append(key)

    if picked:
        lines.append("\n版本差异（只列不同项，相同参数已隐藏）：")
        for key in picked[:_VARIANT_DIFF_KEYS_MAX]:
            cells = [f"{_MARKS[idx]} {value}" for idx, value in enumerate(_vector(key))]
            lines.append(f"{labels[key]}：" + "｜".join(cells))
        if len(picked) > _VARIANT_DIFF_KEYS_MAX:
            lines.append(f"（另有 {len(picked) - _VARIANT_DIFF_KEYS_MAX} 项差异未列出，加入对比可查看完整表）")
    else:
        lines.append("\n各版本在已收录的参数上完全一致；差异可能只在配色或选装包（官方资料未披露）。")

    # 各版本一致的关键项（举 3 个，帮助用户确认「同一台车的不同版本」）
    picks = [
        k for k, m in keys.items()
        if len(m) == len(shown)
        and len({ident for ident, _ in m.values()}) == 1
        and any(kw in k[1] for kw in ("长*宽*高", "轴距", "座位数", "车身结构"))
    ][:3]
    if picks:
        sample = [f"{labels[key]} {next(iter(keys[key].values()))[1]}" for key in picks]
        lines.append("各版本一致：" + "；".join(sample))

    out_rows = [
        {
            "variant_id": variant.id,
            "series_id": series.id,
            "series_name": series.name,
            "brand_name": brand.name if brand else "",
            "display_name": variant.display_name,
            "energy_type": variant.energy_type,
            "price_cny": price,
            "official_page_url": series.official_page_url,
            "source_id": variant.source_id,
        }
        for variant, price in shown
    ]
    lines.append("\n" + footer)
    return "\n".join(lines), out_rows
