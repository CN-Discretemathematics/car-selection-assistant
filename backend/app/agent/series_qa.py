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
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import services as catalog
from app.catalog.series_constraints import PARAM_KEYS
from app.catalog.series_index import (
    HEADLINE_ORDER,
    HEADLINE_PREFIX as _HEADLINE_PREFIX,
    normalize_name,
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
#: 同一事实键跨款型的去重取值上限。
#: 2026-10-05（N6-B，用户拍板「参数追问列全档」）：此前是 2，实测把头条那个值切掉了。
#: 现在正常档位数不再受它约束；它只作**病态输入兜底**（例如某个脏键出了十几档），
#: 超限时仍会如实披露「共 N 个，只列前 M 个」——截断可以，但截断必须说出来。
_PROBE_VALUE_COERCE = 8

#: 核心参数那一行的前缀见 `series_index.HEADLINE_PREFIX`（上面 import 为
#: `_HEADLINE_PREFIX`）——车系问答卡片与 RAG 车系摘要切片共用同一常量，
#: 两个展示点不可能再各说各话。

#: 尺寸的事实键（text 模式，见 `HEADLINE_SPECS`）
_SIZE_FACT_KEY = "长*宽*高(mm)"

# 探针维度 → 用户可读名（v3 不可回答题诚实性标注：问了但 DB 完全没有的维度，
# 必须显式回答「官方资料未披露」——评测 v3 拒答判定 0/60 通过暴露的缺失）
_PARAM_DIM_LABELS: dict[str, str] = {
    r"(续航|能跑多少|跑多远)": "续航",
    r"(油耗|电耗|能耗|耗油|费油|省电)": "油耗/电耗",
    r"(空间|轴距|车长|尺寸|后备箱|行李厢)": "空间尺寸",
    r"(动力|功率|马力|扭矩|加速|零百|几秒|推背)": "动力参数",
    r"(电池|充电|快充|慢充)": "电池与充电",
    r"(安全|气囊|主动刹车|碰撞)": "安全配置",
    r"(悬架|悬挂|底盘|四驱|越野|操控)": "底盘与驱动",
    r"(智驾|辅助驾驶|自动驾驶|车机|芯片|屏幕|音响|抬头显示|雷达|摄像头|泊车)": "智驾与座舱",
    r"(座椅|空调|天窗|冰箱|隔音|按摩|通风|加热|彩电|沙发)": "舒适配置",
}
# 汽车之家配置表的特征标记值 → 用户可读表述（●=标配、○=选装；- 已在跳过表内）
_FEATURE_VALUE_LABEL = {"●": "有（标配）", "○": "选装"}

#: 键尾括号（仅用户可见文案用；匹配逻辑仍用原键）
_KEY_TAIL_PAREN_RE = re.compile(r"[（(]([^（）()]*)[)）]\s*$")
#: 括号内容看起来是单位的样子（km / kWh / L/100km / s / mm / Ps / % …）
_ASCII_UNIT_RE = re.compile(r"^[A-Za-z0-9%·²³/°]+$")
#: 中文单位词——`unit_from_key` 只认 ASCII（「电池快充时间(小时)」它认不出「小时」）
_CN_UNIT_WORDS = frozenset({"小时", "分钟", "秒", "英寸", "吋", "匹", "千瓦", "牛米", "个"})


def display_fact_key(key: str) -> str:
    """用户可见的参数名：剥掉尾部**确实是单位**的括号。

    用户此前看到的是「轴距(mm) = 2650 mm」「前备厢容积(L) = 70 L」——单位在标签和
    值上各写了一遍。实测 877 车系 × 10 组提问共 32347 条渲染行，**15503 条（47.9%）**
    是这个形态（用户 2026-10-05 拍板「只剥确实是单位的尾部括号」）。

    **只剥单位，不剥名字**。库里同时存在括号里是中文名字的键：
    `540°全景影像系统(带透明底盘)`、`M碳陶瓷高性能卡钳（金色卡钳）`、
    `L2级组合驾驶辅助包（限时免费）`——剥掉就丢了真实信息。
    判据：括号内容要么是 ASCII 单位形态，要么落在中文单位词表里，否则原样返回。
    """
    matched = _KEY_TAIL_PAREN_RE.search(key)
    if not matched:
        return key
    inner = matched.group(1).strip()
    if inner and (inner in _CN_UNIT_WORDS or _ASCII_UNIT_RE.match(inner)):
        return key[: matched.start()].strip()
    return key

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


def missing_param_labels(
    facts: list[tuple[str, str, str | None, str | None]], message: str
) -> list[str]:
    """提问命中了参数探针维度、但该车系全量事实里没有任何对应键 → 返回可读维度名。

    M-R10 的探针只处理「DB 有参数」的情形；DB 完全没有该维度时会沉默跳过，
    用户得到一份不回应问题的车系画像（评测 v3 不可回答题拒答判定 0/60 通过暴露）。
    诚实性原则要求这里显式回答「官方资料未披露」。
    """
    known_keys = {row[0] for row in facts}
    labels: list[str] = []
    for query_re, key_re in _PARAM_PROBES:
        if not re.search(query_re, message):
            continue
        if any(re.search(key_re, key) for key in known_keys if key not in _PROBE_SKIP_KEYS):
            continue  # 该维度车系有数据，由 probe_facts 正常作答
        label = _PARAM_DIM_LABELS.get(query_re)
        if label and label not in labels:
            labels.append(label)
    return labels


def asked_missing_param_note(
    facts: list[tuple[str, str, str | None, str | None]],
    message: str,
    missing_dims: list[str] | None = None,
) -> str | None:
    """按键级未披露提示（v6）：消息点名了具体参数键、但该车系在售款型均无该键。

    与 missing_param_labels（维度级）互补：维度有数据（如 WLTC 油耗）但问的是
    另一个键（如 CLTC 纯电续航）时，probe_facts 会答兄弟键、用户问的键被静默
    跳过——诚实性原则要求显式标注「官方资料未披露」。
    只匹配 PARAM_KEYS 的用户可读问法（生成器/前端同源），避免误伤泛问（「续航是多少」）；
    维度整体缺失的键由 missing_param_labels 兜底，此处跳过避免重复。
    """
    known_keys = {row[0] for row in facts if row[1] not in _PROBE_SKIP_VALUES}
    missing_dims = set(missing_dims or [])
    missing: list[str] = []
    # 评审 C10(b)：归一化匹配——用户自然写法「CLTC纯电续航」（无空格）与问法
    # 「CLTC 纯电续航」等价，逐字子串匹配会漏触发按键提示
    norm_message = normalize_name(message)
    for key, phrase in PARAM_KEYS:
        norm_phrase = normalize_name(phrase)
        if norm_phrase not in norm_message and normalize_name(key) not in norm_message:
            continue
        if key in known_keys:
            continue
        dim = _key_dimension(key)
        if dim and dim in missing_dims:
            continue  # 维度级兜底已覆盖
        if phrase in missing:
            continue
        missing.append(phrase)
    return f"{'、'.join(missing)}：官方资料未披露。" if missing else None


def _key_dimension(key: str) -> str | None:
    """事实键所属的探针维度可读名（按 _PARAM_PROBES 的 key_re 顺序首个命中）。"""
    for query_re, key_re in _PARAM_PROBES:
        if re.search(key_re, key):
            return _PARAM_DIM_LABELS.get(query_re)
    return None

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
    shown_keys = matched_keys[: _PROBE_MAX_KEYS]
    for key in shown_keys:
        entries = values_by_key.get(key) or []
        if not entries:
            continue
        rendered: list[str] = []
        # N6-B（用户拍板「参数追问列全档」）：此前只列前 _PROBE_VALUE_MAX(=2) 个去重值，
        # 与「核心参数」那一行（取极值）并存时两个口径打架——问「星愿续航多少」会
        # 同时看到「480 km」（头条，取最大）和「310 / 410」（追问，取前两个）。
        # 现在核心参数行已标「全系极值」且多档给区间，两边语义都交代清楚了，追问侧就**列全档**，
        # 不再自己截断。`coerce` 仍作为**病态输入**的兜底（见下），但正常档位数
        # （实测多在 2~4 档）不再触发。
        entries_all = entries if len(entries) <= _PROBE_VALUE_COERCE else entries[:_PROBE_VALUE_COERCE]
        for value, unit, cycle in entries_all:
            value = _FEATURE_VALUE_LABEL.get(value, value)  # ● → 有（标配）、○ → 选装
            unit = unit or unit_from_key(key) or ""
            if unit and value.lower().endswith(unit.lower()):
                unit = ""
            rendered.append(f"{value}{f' {unit}' if unit else ''}{f'（{cycle}）' if cycle else ''}")
        # 2026-10-05（N6-A）：截断**必须披露**。
        # 实拍原样（星愿，问「续航和电池容量分别是多少」）：
        #   核心参数：… 续航 480 km（CLTC）；电池 47.14 kWh
        #   你问到的相关参数：CLTC纯电续航里程(km) = 310 km（CLTC） / 410 km（CLTC）（不同款型存在差异）
        # 库内实有 3 个续航档（310/410/480），`entries[:2]` 恰好把**头条那个 480** 切掉，
        # 而旧文案只说「不同款型存在差异」——用户以为看到的就是全部。
        # 这与本轮 P1-3（佐证被 `text[:60]` 腰斩成「级别 = 紧…」）是同一类缺陷：
        # **截断了但没说截断**。这里补足「共几个、只列了几个」。
        if len(entries) > len(entries_all):
            suffix = f"（共 {len(entries)} 个取值，此处只列前 {len(entries_all)} 个）"
        elif len(entries) > 1:
            suffix = "（不同款型存在差异）"
        else:
            suffix = ""
        lines.append(f"{display_fact_key(key)} = {' / '.join(rendered)}{suffix}")
    # 2026-10-05（PR #66 审查遗留）：**渲染行数为 0 时不得只留披露行**。
    # 前 N 个命中键的取值全是无信息量值（暂无/-/--/未知）时，循环会全部 `continue`，
    # 只剩披露行，上游拼成「你问到的相关参数：（另有 1 个相关参数未列出）。」
    # ——声称「你问到的参数」却一条都没列。审查实测全库 877 车系 × 8 组提问命中 0 次
    # （非生产可达），但这句话在任何产品口径下都不通，属**决策无关**的兜底。
    if lines and len(matched_keys) > len(shown_keys):
        # 同理：命中了但没展示的键也要说，否则用户以为那就是全部相关参数
        lines.append(f"（另有 {len(matched_keys) - len(shown_keys)} 个相关参数未列出）")
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


#: 配置表里表示「**标配**」的标记值（审查实测：18 个亮点键在全库只出现
#: `●` / `○` / `支持` 三种；`○` 是**选装**、`-`/`暂无` 是未配备）。
#: 只有这些值才计入「N 款中 M 款配备」的 M——把 ○ 算进去会让覆盖率虚高一截
#: （实测记忆泊车 ○=333 ≈ ●=504 的四成）。
_EQUIPPED_VALUES = frozenset({"●", "支持"})


def _is_equipped(values: set[str] | None) -> bool:
    """某款型是否**标配**了这项配置。

    - 同一款可能同时有 `●` 行与 `○` 行（不同配置来源），只要有任一标配行即算配备；
    - `○` 单独出现 = **选装**，不算配备（用户买标配款拿不到）；
    - 空值 / `-` / `暂无` / `未知` 一律不算。
    """
    return bool(values and (values & _EQUIPPED_VALUES))


def series_highlights(db: Session, series: VehicleSeries) -> list[str]:
    """车系亮点配置，**带款型覆盖率标注**（最多 5 项）。

    2026-10-05 生产实测（N1，用户拍板「标注覆盖率」）：此前只 select `fact_key`，
    于是**任何一款配备就整系算有**。实测星愿在售 6 款，记忆泊车仅 **1/6**、遥控泊车
    **2/6**、主动安全 **2/6**，而车系卡片把三项全列为「亮点配置」——6 款里 5 款
    没有记忆泊车的用户被告知有。

    交叉验证（同一轮对话内）：问「星愿不同版本有什么区别」时，版本差异路径对**同一份
    数据**逐款标「①~⑤ 官方资料未披露、⑥ 有」——口径是诚实的。两条路径对同一件事
    给出不同答案，错的那一条。

    口径：`<项名>（在售 N 款中 M 款配备）`。`total` 取该车系**在售款型数**，
    `M` 只统计**标配**款型。两个口径都被独立审查实测纠错过：

    1. **○（选装）不是配备**。本文件 `_FEATURE_VALUE_LABEL = {"●": "有（标配）",
       "○": "选装"}`。审查实测真实库：记忆泊车全局 ●=504 而 ○=333——若把 ○ 也算
       「配备」，四成的计数是选装，于是会出现「0 款标配却写『在售 16 款中 2 款配备』」。
    2. **按款型去重，不按事实行数**。`SpecFact` 对 `(variant_id, fact_key)` **没有唯一
       约束**（全库 36241 组重复），按行累加会让 n 超过 total；一旦 n ≥ total，
       标注条件 `n < total` 不成立，于是**静默退回裸标签**——审查实测 1841 条亮点
       带标注、1841 条不带，N1 要治的病只修掉了一部分。
    """
    variants = catalog.series_variants(db, series.id, on_sale_only=True)
    total = len(variants)
    if not total:
        return []
    ids = [v.id for v in variants]
    # set 而非 Counter：(variant_id, fact_key) 可能有多行重复，且同一款可能既有
    # ● 行又有 ○ 行——我们要的是「这款配备吗」，所以按 (variant, key) 去重后再判值。
    per_variant: dict[int, dict[str, set[str]]] = {}
    for vid, key, value in db.execute(
        select(SpecFact.variant_id, SpecFact.fact_key, SpecFact.fact_value)
        .where(SpecFact.variant_id.in_(ids))
    ).all():
        per_variant.setdefault(vid, {}).setdefault(str(key), set()).add((value or "").strip())
    out: list[str] = []
    for key, label in _FEATURE_HIGHLIGHTS:
        n = sum(
            1 for per_variant in per_variant.values()
            if _is_equipped(per_variant.get(key))
        )
        if n:
            out.append(label if n >= total else f"{label}（在售 {total} 款中 {n} 款配备）")
        if len(out) >= 5:
            break
    return out


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
    # 口径标签与前端一致：「门户口径」是内部叫法，用户看到的是「榜单口径」（2026-09-14）
    label = "榜单口径" if sales.sales_type == "portal" else "零售口径"
    return f"；{sales.month} 月销量 {sales.sales_count:,} 辆（{label}）"


def _price_overlap_verdict(db: Session, first: VehicleSeries, second: VehicleSeries) -> str:
    """两车价格区间的**实际**关系 → 小结措辞（N2）。

    返回可直接嵌进句子的片段：
    - 区间重叠  → 「价格区间高度重叠」
    - 完全不重叠 → 「价格区间没有重叠」
    - 任一方价格未披露 → 空串（**什么都不说**，而不是编一个结论）

    口径说明：判定用「重叠」而非「差异大小」，因为用户在这两个选项之间真正需要
    的答案是「是不是同一价位的车」；给出「差异明显」却不给数字，正是本次实测里
    最让人困惑的地方。
    """
    a = catalog.series_price_range(db, first.id)
    b = catalog.series_price_range(db, second.id)
    if not a or not b:
        return ""
    a_lo, a_hi = a
    b_lo, b_hi = b
    if a_lo is None or a_hi is None or b_lo is None or b_hi is None:
        return ""
    if a_lo <= b_hi and b_lo <= a_hi:
        return "、价格区间高度重叠"
    return "、价格区间没有重叠"


def _series_header(db: Session, series: VehicleSeries, brand: Brand | None) -> str:
    return (
        f"{series.positioning or '定位未标注'} · "
        f"{_BODY_LABEL.get(series.body_type or '', series.body_type or '车身未标注')} · "
        f"{' / '.join(_ENERGY_LABEL.get(t, t) for t in (series.energy_types or [])) or '能源未标注'} · "
        f"官方指导价 {_price_text(db, series)}"
    )


def size_line(db: Session, series: VehicleSeries) -> str | None:
    """「尺寸」这一行的展示文本：单档照旧；多档取**在售款型里的众数**并标覆盖率。

    为什么单独查、不并进 `rank_headlines`：
      1. 覆盖率必须**按款型**去重——`SpecFact` 对 `(variant_id, fact_key)` 无唯一约束
         （全库 36241 组重复），按事实行累加会虚高。N1 已经吃过一次这个亏。
         而 `rank_headlines` 拿到的行里没有 `variant_id`。
      2. 尺寸是 text 模式「取首值」，那个首值取决于数据库返回顺序，本质是**任取**。
         实测 312/877（35.6%）的车系尺寸多档，其中 113 个（36.2%）众数与首值不同——
         改完这 113 个会真的换一个更有代表性的值。

    并列第一时（实测 79 个车系）取**事实表里出现得最早**的那个：并列本就无从分优劣，
    保持与改动前一致比换个任意排序更稳。覆盖率标注会把「N 款中 M 款」如实说清楚。

    `rank_headlines` 侧的尺寸行为**不动**（仍取首值、不参与区间），
    本函数只在渲染时覆盖它，所以离线索引与既有单测都不受影响。
    """
    rows = db.execute(
        select(VehicleVariant.id, SpecFact.fact_value, SpecFact.unit)
        .join(SpecFact, SpecFact.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.series_id == series.id,
            VehicleVariant.status == "on_sale",
            SpecFact.fact_key == _SIZE_FACT_KEY,
        )
    ).all()
    if not rows:
        return None

    first_by_variant: dict[int, str] = {}
    for variant_id, value, row_unit in rows:
        if not value:
            continue
        # 行上的 unit 优先（与 `rank_headlines` 同口径），没有才从键名推
        unit = (row_unit or "").strip() or (unit_from_key(_SIZE_FACT_KEY) or "")
        text = str(value).strip()
        if unit and text.lower().endswith(unit.lower()):
            text = text[: -len(unit)].strip()  # 值自带单位，别再拼一遍
        first_by_variant.setdefault(int(variant_id), f"{text} {unit}".strip())
    if not first_by_variant:
        return None

    counter = Counter(first_by_variant.values())
    # Counter 保插入序，most_common 在同票时保留「先出现」的那个（见 docstring）
    top_text, top_count = counter.most_common(1)[0]
    total = len(first_by_variant)
    if len(counter) < 2:
        return top_text  # 单档：top_text 已含单位，不要再拼一遍（否则「mm mm」）
    return f"{top_text}（在售 {total} 款中 {top_count} 款为此尺寸）"


def _head_with_size(
    db: Session, series: VehicleSeries, head: dict[str, str]
) -> dict[str, str]:
    """把 `rank_headlines` 的尺寸项换成 `size_line` 的口径（众数 + 覆盖率）。

    三个展示点（单车系块、对比块、逐项对比行）都必须过这一道——
    少一个就会在同一条回答里出现两个互相矛盾的尺寸。
    """
    size = size_line(db, series)
    return {**head, "尺寸": size} if size else head


def _describe(db: Session, series: VehicleSeries, brand: Brand | None) -> str:
    name = display_name(series, brand)
    parts = [f"「{name}」：{_series_header(db, series, brand)}"]
    head = _head_with_size(db, series, series_headline(db, series))
    if head:
        order = [label for label in HEADLINE_ORDER if label in head]
        parts.append(
            _HEADLINE_PREFIX + "；".join(f"{label} {head[label]}" for label in order)
        )
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
        # 2026-10-05（N5）：此前是 `关于「X」：` + `_describe()`，而 `_describe()`
        # 自己就以「X：」开头，于是站上同一句里车系名出现两次：
        #   关于「东风奕派eπ007」：
        #   「东风奕派eπ007」：中大型车 · 轿车 · …
        # `_describe` 是唯一调用点，故去掉外层重复的全名即可，不改它的内部结构。
        parts = [_describe(db, series, brand)]
        # 按需参数查找（评审 M-R10）：提问命中的维度从全量 DB 事实直接取值
        facts = series_fact_rows(db, [series.id]).get(series.id, [])
        probed = probe_facts(facts, message)
        if probed:
            parts.append("你问到的相关参数：" + "；".join(probed) + "。")
        missing_dims = missing_param_labels(facts, message)
        if missing_dims:
            # 诚实性兜底（评测 v3）：问到的维度 DB 完全没有 → 显式「官方资料未披露」，
            # 绝不沉默跳过，也绝不编造
            parts.append("你问到的" + "、".join(missing_dims) + "：官方资料未披露。")
        key_note = asked_missing_param_note(facts, message, missing_dims)
        if key_note:
            parts.append("你问到的" + key_note)
        parts.append(footer)
        return "\n".join(parts)

    # 批量核心参数（评审 P2：此前多车系对比逐车系单查，N 次查询）
    facts_by_series = series_fact_rows(db, [s.id for s, _ in resolved])
    heads_map = rank_headlines(facts_by_series)

    blocks: list[str] = ["你说的这两款车我先放在一起看："]
    for series, brand in resolved:
        name = display_name(series, brand)
        blocks.append(f"\n【{name}】{_series_header(db, series, brand)}")
        head = _head_with_size(db, series, heads_map.get(series.id, {}))
        if head:
            order = [label for label in HEADLINE_ORDER if label in head]
            blocks.append(
                "  " + _HEADLINE_PREFIX + "；".join(f"{label} {head[label]}" for label in order)
            )
        # 按需参数查找（评审 M-R10）：对比语境下同样回答问到的具体参数
        series_facts = facts_by_series.get(series.id, [])
        probed = probe_facts(series_facts, message)
        if probed:
            blocks.append("  你问到的相关参数：" + "；".join(probed))
        # 诚实性兜底对齐单车系路径（评审 C11）：某车系在问到的维度/按键上无数据时
        # 显式标注，不沉默跳过
        missing_dims = missing_param_labels(series_facts, message)
        if missing_dims:
            blocks.append("  你问到的" + "、".join(missing_dims) + "：官方资料未披露。")
        key_note = asked_missing_param_note(series_facts, message, missing_dims)
        if key_note:
            blocks.append("  你问到的" + key_note)
        highlights = series_highlights(db, series)
        if highlights:
            blocks.append("  亮点配置：" + "、".join(highlights))
        sales = _sales_text(db, series)
        if sales:
            blocks.append(f"  {sales.lstrip('；')}")

    # 逐项对比（双方都有数据的量纲）
    # 尺寸必须走 `size_line`（众数+覆盖率）而不是 `heads_map` 的首值，否则
    # 上面那一块写「5050*1960*1505 mm（在售 6 款中 4 款为此尺寸）」、
    # 下面这行写「4995*1910*1495 mm」——**同一条回答里自相矛盾**。
    heads = [_head_with_size(db, series, heads_map.get(series.id, {}))
             for series, _ in resolved]
    diff: list[str] = []
    for label in HEADLINE_ORDER:
        values = [h.get(label) for h in heads]
        if all(values):
            diff.append(f"{label}：{values[0]} vs {values[1]}")
    if diff:
        blocks.append("\n同量纲参数对比：" + "；".join(diff))

    # 客观小结（同级/异级判断，不含主观推荐）
    #
    # ⚠️ N2（2026-10-05 生产实测 + 用户拍板「真的去比」）：此前这里的「价格区间差异明显」
    # 是**写死的字符串**——只要两车 positioning 不同就走该分支，而代码从头到尾
    # **没有比较过价格**。实拍就当场自相矛盾：
    #   前文列出  银河星愿 6.48-9.48 万  vs  零跑A10 6.58-8.68 万（高度重叠）
    #   结尾却写  「两款车级别与价格区间差异明显」
    # 这是本项目最不该出现的形状：**结论没有推导过程**。现在真的去比价格区间，
    # 而且只依据实际算出的结论选择措辞；算不出（价格未披露）时**什么都不说**。
    first, second = resolved[0][0], resolved[1][0]
    same_class = first.positioning and first.positioning == second.positioning
    price_verdict = _price_overlap_verdict(db, first, second)
    if same_class:
        blocks.append(
            "小结：两款车同属「" + first.positioning + "」级别" + price_verdict + "；"
            "可以按用车场景（通勤/家庭/长途）和预算取舍——告诉我你的预算和主要用途，我按库内参数帮你细比。"
        )
    else:
        blocks.append(
            "小结：两款车定位不同（「"
            + (first.positioning or "未标注") + "」vs「"
            + (second.positioning or "未标注") + "」）"
            + price_verdict + "，直接比「谁更好」意义不大；"
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


#: SpecFact 里唯一属于**硬参数**的类目；其余（内部配置/安全配置/外部配置/
#: 智能辅助驾驶/操控配置/个性化）都是**配置项**。实测库内分布：参数信息 44 万条，
#: 其余六类合计约 30 万条——两者量级相当，混排才看不出差异（2026-10-05 N3）。
_SPEC_CATEGORY = "参数信息"


def build_variant_diff_answer(
    db: Session, series: VehicleSeries, brand: Brand | None, message: str = ""
) -> tuple[str, list[dict]]:
    """同一车系内「版本差异」确定性回答。

    返回 (文本, 版本行)。版本行供前端渲染候选卡片与「加入对比」动作。
    事实全部来自在售 SKU 的 SpecFact / OfficialPrice；无数据时如实说明（不提供外部跳转，
    官方车型页链接已于 2026-09-16 删除，见 docs/deployment.md §8）。
    """
    name = display_name(series, brand)
    rows: list[tuple[VehicleVariant, float | None]] = []
    # 2026-10-02（P5.2 / H4）：价格批量取。此前逐款型 SELECT，一个 8 款型的车系
    # 就是 8 次往返；口径与 variant_current_price 完全一致。
    on_sale = catalog.series_variants(db, series.id, on_sale_only=True)
    prices = catalog.variants_current_prices(db, [v.id for v in on_sale])
    for variant in on_sale:
        price = prices.get(variant.id)
        rows.append((variant, float(price.price_cny) if price else None))
    rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0.0))

    archived = False  # 无在售款型时降级展示库内归档款型（标注停售）
    if not rows:
        # 2026-09 部署实测：部分车系（护卫舰07/宝骏云朵/凯美瑞旧代次等 31 个）款型
        # 全为停售——数据本身完整，直接展示并标注停售，比「未收录」死胡同更有用
        all_variants = catalog.series_variants(db, series.id, on_sale_only=False)
        all_prices = catalog.variants_current_prices(db, [v.id for v in all_variants])
        for variant in all_variants:
            price = all_prices.get(variant.id)
            rows.append((variant, float(price.price_cny) if price else None))
        rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0.0))
        archived = True
        if not rows:  # 连款型都没有（极少数）：保留原说明
            text = (
                f"【{name}】库内暂未收录该车型的款型数据。"
                "可能原因：数据源尚未收录。\n"
                + "以上口径：只陈列数据库既有事实，绝不编造。"
            )
            return text, []

    footer = (
        "以上为数据库在售款型的官方指导价与配置事实；标注「"
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
    # 2026-10-02（P5.2 / H4）：事实批量取，shown 有几款型就只查一次
    facts_by_variant = catalog.variants_facts(db, [v.id for v, _ in shown])
    keys: dict[tuple[str, str], dict[int, tuple[tuple, str]]] = {}
    labels: dict[tuple[str, str], str] = {}
    for variant, _ in shown:
        for fact in facts_by_variant.get(variant.id, []):
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
        # 2026-10-05（N3）：配置项与硬参数分块列出。此前两者混在一张表里，
        # 站上原样：续航/电池/功率/扭矩/快充/快充/记忆泊车/辅助泊车/座椅电动调节/
        # 后座出风口/**后电动机型号 TZ160XS001**/整备质量 —— 想找「哪个版本有记忆泊车」
        # 得在一堆 kW·min·kg 里翻，而「后电动机型号」这类内部件对选车几乎无意义，
        # 却占一整行的显著位置。
        #
        # 刻意**只拆渲染、不动入选**：哪些条目进表仍由 `_VARIANT_DIFF_PRIORITY` 与
        # `_VARIANT_DIFF_KEYS_MAX` 决定（评审 M2 的口径：部分版本缺收录也算差异）。
        # 改入选集合会同时改变既有基线与「另有 N 项未列出」的计数，那属于判据变更，
        # 不该由一个排版修正夹带。
        shown_picked = picked[:_VARIANT_DIFF_KEYS_MAX]
        groups = [
            ("配置", [k for k in shown_picked if k[0] != _SPEC_CATEGORY]),
            ("参数", [k for k in shown_picked if k[0] == _SPEC_CATEGORY]),
        ]
        for title, group in groups:
            if not group:
                continue
            lines.append(f"\n版本差异·{title}（只列不同项）：")
            for key in group:
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
            "source_id": variant.source_id,
        }
        for variant, price in shown
    ]
    lines.append("\n" + footer)
    return "\n".join(lines), out_rows
