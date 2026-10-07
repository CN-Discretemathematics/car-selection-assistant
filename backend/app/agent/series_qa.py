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
    active_series_count,
    normalize_name,
    split_key_unit,
    display_name,
    rank_headlines,
    head_with_size,
    series_fact_rows,
    size_line,
    size_lines,
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

#: 键尾括号的判定**不在本模块**——统一走 `series_index.split_key_unit`，
#: 与 `probe_facts` 给值补单位用的是同一个函数（2026-10-05 审查 P1-1：
#: 两个独立识别器会让「电池快充时间(小时)」与「(分钟)」剥成同名且都无单位）。


def display_fact_key(key: str) -> str:
    """用户可见的参数名：剥掉尾部**确实是单位**的括号。

    用户此前看到的是「轴距(mm) = 2650 mm」「前备厢容积(L) = 70 L」——单位在标签和
    值上各写了一遍。实测 877 车系 × 10 组提问共 32347 条渲染行，**15503 条（47.9%）**
    是这个形态（用户 2026-10-05 拍板「只剥确实是单位的尾部括号」）。

    **判据来自 `series_index.split_key_unit`——与值侧补单位用的是同一个函数**。
    2026-10-05 审查实锤：此前这里和 `unit_from_key` 是两个各自独立的识别器
    （本函数认全角括号 + 中文单位词表，`unit_from_key` 只认半角 + ASCII），
    于是 `电池快充时间(小时)` 与 `电池快充时间(分钟)` 都被剥成「电池快充时间」，
    而值侧又都补不出单位——同一回答里两个**量纲差 60 倍**的东西同名且都无单位
    （实测 719 行；另有「排量(L)」vs「排量(mL)」同病）。

    判据只在**末尾**括号上生效：中部括号是名字的一部分
    （`540°全景影像系统(带透明底盘)`、`M碳陶瓷高性能卡钳（金色卡钳）`、
    `L2级组合驾驶辅助包（限时免费）`、`AI空气投影（限时1500）`），剥掉就丢真实信息。
    """
    return split_key_unit(key)[0]

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
    # 剥括号会把**不同量纲**的键压成同一个名字：全库实测 3 组——
    #   电池快充时间(小时) / 电池快充时间(分钟)   -> 都叫「电池快充时间」
    #   排量(L) / 排量(mL)                        -> 都叫「排量」
    #   全地形轮胎 / 全地形轮胎（AT）              -> 都叫「全地形轮胎」
    # 值上会各自带单位（`0.35 小时` vs `21 分钟`）所以不丢信息，但两行同名仍容易误读。
    # 因此：**同一个展示名被 >1 个键占用时，这些键一律保留原样**。
    name_hits = Counter(display_fact_key(k) for k in shown_keys)
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
            # 2026-10-05（审查 P1-1）：此处必须与 `display_fact_key` 用**同一个**
            # 键尾解析（`split_key_unit`），否则标签剥了「(小时)」而值侧补不出单位，
            # 单位就彻底消失——`电池快充时间(小时)` 与 `(分钟)` 还会剥成同名。
            unit = unit or split_key_unit(key)[1] or ""
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
        name = display_fact_key(key)
        if name_hits[name] > 1:
            name = key  # 同名歧义 → 保留原键，让量纲在标签上就分得开
        lines.append(f"{name} = {' / '.join(rendered)}{suffix}")
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


def _multi_price_verdict(db: Session, series_list: list[VehicleSeries]) -> str:
    """N 车系的价格关系 → 小结措辞（`resolve_series` 最多解析 **4 个**车系）。

    两车沿用 `_price_overlap_verdict` 的成对口径；三车及以上改说**整体跨度** +
    「有没有任意一对重叠」——只报跨度不报重叠会漏掉「贵的和便宜的挨着、中间那个
    另算」的情况，只报重叠不报跨度则用户不知道这堆车大概多少钱。
    任一车系价格未披露即返回空串：**什么都不说**，而不是拿已知的两台编一个结论。
    """
    if len(series_list) == 2:
        return _price_overlap_verdict(db, series_list[0], series_list[1])
    spans: list[tuple[float, float]] = []
    for series in series_list:
        bounds = catalog.series_price_range(db, series.id)
        if not bounds or bounds[0] is None or bounds[1] is None:
            return ""
        spans.append((bounds[0], bounds[1]))
    low = min(s for s, _ in spans) / 10000
    high = max(e for _, e in spans) / 10000
    overlap = any(
        a_lo <= b_hi and b_lo <= a_hi
        for i, (a_lo, a_hi) in enumerate(spans)
        for b_lo, b_hi in spans[i + 1:]
    )
    span_text = f"{low:g}-{high:g} 万" if high != low else f"{low:g} 万"
    tail = "其中有价格区间重叠" if overlap else "价格区间互不重叠"
    return f"、指导价跨度 {span_text}、{tail}"


def _count_phrase(count: int) -> str:
    """「这两款车 / 这三款车 / 这四款车 / 这 5 款车」。

    对比链路此前**通篇写死「两款车」**（开头、`values[0] vs values[1]`、小结），
    而 `resolve_series` 明确「最多 4 个」——用户点名三款车时，第三款会被
    **静默丢出对比行**，小结还写「两款车定位不同」，用户完全看不出来。
    """
    return {2: "这两款车", 3: "这三款车", 4: "这四款车"}.get(count, f"这 {count} 款车")


def _summary_for_many(
    db: Session, resolved: list[tuple[VehicleSeries, Brand | None]]
) -> str:
    """N 车系（>=3）的小结：按**全体**定位与价格说话，不假装只比了两台。"""
    series_list = [series for series, _ in resolved]
    names = [display_name(s, b) for s, b in resolved]
    classes = [s.positioning or "未标注" for s in series_list]
    verdict = _multi_price_verdict(db, series_list)
    if len(set(classes)) == 1:
        return (
            f"小结：这几款车同属「{classes[0]}」级别{verdict}；"
            "可以按用车场景（通勤/家庭/长途）和预算取舍——告诉我你的预算和主要用途，"
            "我按库内参数帮你细比。"
        )
    spread = "、".join(f"{name}是「{cls}」" for name, cls in zip(names, classes, strict=True))
    return (
        f"小结：这几款车定位不一致（{spread}）{verdict}，直接比「谁更好」意义不大；"
        "更合适的做法是按预算与用途缩小范围——告诉我预算和主要用途，我可以帮你筛真正同档的候选。"
    )


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
    head = head_with_size(size_line(db, series), series_headline(db, series))
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


#: 品牌反问里最多给几个可点的车系样例。
_BRAND_SAMPLE_LIMIT = 3


def _series_name_spans(
    message: str, resolved: list[tuple[VehicleSeries, Brand | None]]
) -> list[tuple[int, int]]:
    """已解析车系名在归一化消息里占据的区间（半开），含「品牌+车系」拼接形式。"""
    normalized = normalize_name(message)
    spans: list[tuple[int, int]] = []
    for series, brand in resolved:
        names = [series.name, *(series.aliases or [])]
        if brand is not None:
            names.append(f"{brand.name}{series.name}")
        for raw in names:
            needle = normalize_name(raw or "")
            if not needle:
                continue
            start = normalized.find(needle)
            while start != -1:
                spans.append((start, start + len(needle)))
                start = normalized.find(needle, start + 1)
    return spans


def _brand_appears_outside(
    normalized: str, label: str, spans: list[tuple[int, int]]
) -> bool:
    """品牌词是否**至少有一次**出现落在所有车系名区间之外。"""
    needle = normalize_name(label)
    if not needle:
        return False
    start = normalized.find(needle)
    while start != -1:
        end = start + len(needle)
        if not any(lo <= start and end <= hi for lo, hi in spans):
            return True
        start = normalized.find(needle, start + 1)
    return False

#: 被上限截掉的车系名最多列几个（再多的用「等共 N 台」收尾，不把回答撑成长名单）。
_DROPPED_NAME_LIMIT = 5


def _dropped_note(
    db: Session,
    resolved: list[tuple[VehicleSeries, Brand | None]],
    message: str,
) -> str:
    """点名超过上限时，**明说**哪些车系没被放进这次比较（2026-10-07 用户拍板）。

    此前 `resolve_series` 硬编码 `[:4]`，用户点名 5 台时第 5 台被**静默丢掉**——
    与同批修的「品牌被整段吞掉」是同一类错误：不响、用户以为 5 台都参与了。

    只在真的触到上限时才重算一次解析；**重算结果与传进来的 `resolved` 必须逐 id
    相同**才说话，否则宁可不提——不拿一个可能对不上的名单去糊弄用户。
    """
    from app.catalog.series_index import RESOLVE_SERIES_LIMIT, resolve_series_with_dropped

    if len(resolved) < RESOLVE_SERIES_LIMIT:
        return ""
    again, dropped = resolve_series_with_dropped(db, message)
    if not dropped or [s.id for s, _ in again] != [s.id for s, _ in resolved]:
        return ""
    names = "、".join(f"「{n}」" for n in dropped[:_DROPPED_NAME_LIMIT])
    more = f"，等共 {len(dropped)} 台" if len(dropped) > _DROPPED_NAME_LIMIT else ""
    # 主语必须是**系统视角**，不能是「你一共提到 N 台」——独立审查实测（2026-10-07）：
    # 用户点 9 个名字、其中一个库里没有（「途观」只有「途观L插电混动」）时，
    # `total = len(resolved) + len(dropped)` 数的是**匹配上且去重后**的车系，
    # 会说出「你一共提到 8 台」——用户点的是 9 个，一对就发现是假话。
    return (
        f"\n下面放在一起看的是我认出的 {len(resolved)} 台车；"
        f"没有放进来的有{names}{more}，可以单独问我。"
    )


def _brand_active_series(db: Session, brand_name: str) -> list[str]:
    """某品牌名对应的在售车系名（按车系 id 升序）。

    先按 `brand_id` 精确取。**取不到时才**用「车系名以该品牌词开头」兜一次——
    真实库里有**空的重复品牌行**：

        「MG」   → 在售车系 = []            ← 空行
        「名爵」 → 在售车系 = [MG4, MG5, MG6, MG7, MG 4X, MG ES5, MG Cyberster]

    MG 与名爵是两个 `brand_id`，MG 的 7 款车全挂在名爵下。只按 `brand_id` 数，
    「MG」算出 0 款，于是「MG 和汉哪个好」会**一个字不提 MG**——而用户明明点名了它。

    兜底**额外要求命中的车系全部挂在同一个品牌行下**。真实库上 MG/名爵、
    长安启源/启源都满足；这条限制是防御性的：万一将来出现一个空品牌行 A，而
    **别的厂商** B 恰好有个车系名以 A 开头，不该把 B 的车算成 A 的。
    """
    from sqlalchemy import select

    from app.common.models import Brand, VehicleSeries

    rows = db.execute(
        select(VehicleSeries.name)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(Brand.name == brand_name, VehicleSeries.active_status == "active")
        .order_by(VehicleSeries.id)
    ).all()
    if rows:
        return [r[0] for r in rows]
    head = normalize_name(brand_name)
    if not head:
        return []
    # 归一化后判定，SQL 只做粗筛。SQLite 的 LIKE 对 ASCII 不区分大小写、
    # 生产 PostgreSQL 区分，所以真正的判据是下面这行 startswith，不是 SQL。
    cands = db.execute(
        select(VehicleSeries.name, VehicleSeries.brand_id)
        .where(
            VehicleSeries.active_status == "active",
            VehicleSeries.name.like(f"{brand_name}%"),
        )
        .order_by(VehicleSeries.id)
    ).all()
    hits = [(name, bid) for name, bid in cands if normalize_name(name).startswith(head)]
    if len({bid for _name, bid in hits}) > 1:
        return []  # 命中车系分属不同品牌行 → 不能算这个品牌的
    if hits:
        return [name for name, _bid in hits]

    # **借用兄弟品牌行**（2026-10-07 用户拍板）。真实库有 5 个**幻影品牌行**——
    # 品牌名在、车系不在，而车挂在**同一家公司另一个品牌行**下：

    #     吉利银河  0 款  ←→  吉利汽车 11 款 / 银河 12 款
    #     零跑汽车  0 款  ←→  零跑     9 款
    #     理想汽车  0 款  ←→  理想     5 款
    #     AITO 问界 0 款  ←→  问界     5 款
    #     待分类（汽车之家销量榜）0 款  ←→  无对应，仍为 0
    #
    # 「车系名以该品牌词开头」这条兼底救不了它们——零跑的车叫「零跑T03」而不是
    # 「零跑汽车T03」。改为：找一个**以本品牌名开头或结尾**的另一个品牌行，借它的车。
    # 取**最长**的那个匹配（「长安启源」上面已经有 6 款，不会走到这里；真走到这里时
    # 「长安启源」与「长安」都匹配，取「长安启源」优先才不会被 26 款的大品牌盖掉）。
    from app.catalog.brands import _load_entries as _brand_entries
    from app.common.models import Brand as _Brand

    raw = brand_name.strip()
    if len(raw) >= 2:
        # 排序键带名字本身，不能只用 `len`：同长度的兄弟来自 `set` 迭代，
        # 顺序随进程哈希随机化（上线前审查实测 seed=2/4/7 时「长安启源」的候选
        # 变成 ['启源','长安']）。款数当时恰好相同，但**答案是谁**不能靠运气。
        siblings = sorted(
            {
                other
                for other, _bid, _label in _brand_entries(db)
                if other != raw
                and len(other) >= 2
                and (raw.startswith(other) or raw.endswith(other))
            },
            key=lambda s: (-len(s), s),
        )
        for sibling in siblings:
            rows_sib = db.execute(
                select(VehicleSeries.name)
                .join(_Brand, VehicleSeries.brand_id == _Brand.id)
                .where(_Brand.name == sibling, VehicleSeries.active_status == "active")
                .order_by(VehicleSeries.id)
            ).all()
            if rows_sib:
                return [r[0] for r in rows_sib]
    return []


def _brand_disclosure(
    db: Session,
    resolved: list[tuple[VehicleSeries, Brand | None]],
    message: str,
) -> str:
    """比较候选里出现**品牌**时的一段补充说明（无则空串）。

    2026-10-06 用户拍板的方案：用户点名的比较对象其实是品牌时，别装看不见，
    查库告诉他那个品牌有几款在售车，请他把车系名说清楚。真实库：

        用户：大众和汉哪个好
        库里：大众有 29 款在售，它不是一款车；汉在库里且参数完整
        回法：先正常回答汉，再在**末尾追加**一段说明

    两个决定性取舍（都是四轮审查逼出来的）：

    1. **只处理一种情形**：品牌词与「按连接词切段、剥掉问句尾巴后的整段」**完全相等**。
       裸型号、款型名、别名一律**不报**——少一句提示的代价，远小于对着问奔驰 GLB AMG
       的用户推销 MG4。判定实现见 `brands.brand_candidates_in_message`，那里记了
       为什么不能用「重建用户写了什么再建区间」那条路。
    2. **追加，不是替代**：调用方必须把它追加在正常回答**之后**。第一版做成替换，
       朗逸的 807 字参数卡被整段丢掉只剩一句反问，用户点名两台一台数据也没有。

    三处调用点（单车系 / 双车系 / ≥3 车系）都要追加，一条都不能漏。
    """
    from app.catalog.brands import brand_candidates_in_message

    if not resolved:
        return ""
    # 品牌是**已解析车系自己的**品牌 → 用户已经点名到车系了，再问一遍是废话
    # （「大众和朗逸哪个好」：朗逸就是大众的车，反问等于把用户刚给的车型名推荐回去）
    resolved_brands = {brand.name for _s, brand in resolved if brand is not None}
    # 品牌词**恰好等于某个已解析车系名**时同样不报。真实库有个车系就叫「MINI」，
    # 它恰好挂在品牌「MINI」下——那是数据巧合，代码里没有任何东西保证它。
    # 合成库里把「MINI」车系挂到别的品牌下，问「MINI值得买吗」就会得到
    # 「MINI 是品牌…它不是一款车」——用户只问了一台车，却被告知它不是车。
    # 遮蔽让「车系名」和「品牌词」在文本层完全不可区分，挡住它的只有这里。
    resolved_brands |= {series.name for series, _b in resolved}
    candidates = sorted(brand_candidates_in_message(db, message) - resolved_brands)
    # **兜底：品牌词在消息里每一次出现都落在已解析车系名的字面范围内 → 剔掉。**
    #
    # 2026-10-07 独立审查实测的一类真误反问：切段字符把**含空格/连字符的车系名**
    # 切碎，碎片恰好等于某个品牌词——「MG Cyberster和汉哪个好」在答案末尾追加
    # 「MG 是品牌，库里有 7 款…它不是一款车」，而答案开头刚报完这台车的完整参数。
    # 真实库 18 个车系中招（MG 4X / MG Cyberster / MG ES5 / iCAR 超级V23 / iCAR V27 /
    # 极狐 阿尔法S5 / 极狐 考拉S …）。
    #
    # `resolved_brands` 那道相减**挡不住**：它按 `brand.name` 减，而库里有**空的重复
    # 品牌行**——「MG」(id=10) 与「名爵」(id=55) 是两行，MG Cyberster 挂在名爵下，
    # 于是「MG」逃过相减。
    #
    # 这里判的是「**每一次**出现都在车系名里」而不是「出现过」：
    # 「北京现代ix35和北京哪个好」里「北京」既出现在车系名内部、又独立出现了一次，
    # 那一次是真的在问品牌，必须保留。
    #
    # 这是**剔除**方向：只会让候选变少，不会新增错误。
    if candidates:
        spans = _series_name_spans(message, resolved)
        if spans:
            normalized = normalize_name(message)
            candidates = [
                label for label in candidates
                if _brand_appears_outside(normalized, label, spans)
            ]
    if not candidates:
        return ""
    # 库里 0 款在售的品牌不反问：说「库里有 0 款在售车」再附几个样例，
    # 本身就是一句自相矛盾的话。
    series_by_brand = {label: _brand_active_series(db, label) for label in candidates}
    candidates = [label for label in candidates if series_by_brand[label]]
    if not candidates:
        return ""
    detail = "；".join(
        f"「{label}」是品牌，库里有 {len(series_by_brand[label])} 款在售车"
        for label in candidates
    )
    samples: list[str] = []
    for label in candidates:
        for series_name in series_by_brand[label]:
            if series_name not in samples:
                samples.append(series_name)
            if len(samples) >= _BRAND_SAMPLE_LIMIT:
                break
        if len(samples) >= _BRAND_SAMPLE_LIMIT:
            break
    hint = f"（比如{'、'.join(samples)}）" if samples else ""
    return f"\n{detail}——它不是一款车。想比哪一款？把车系名给我{hint}，我就能比。"


def build_series_qa_answer(
    db: Session,
    resolved: list[tuple[VehicleSeries, Brand | None]],
    message: str,
) -> str:
    """生成车系问答的确定性回答文本（所有内容来自数据库事实）。"""
    # 2026-10-07 用户拍板：回答里**声明覆盖范围**。此前用户问「大众朗逸和明锐哪个好」
    # 只得到朗逸的回答、没有任何「明锐库里没有」的提示，会以为看全了。说出范围
    # （库内在售 N 个车系）比逐个点名「哪个没有」诚实且零维护成本——要说准某台车缺，
    # 前提是能认出它是个车型名，而库里没有它就需要一份全量车型名录。
    footer = (
        "以上基于汽车之家参数配置页与官方指导价整理"
        f"（范围：库内在售车系 {active_series_count(db)} 个，未收录的车型不在此列），"
        "动态驾驶感受、车主口碑与优惠信息不在数据范围内，具体以品牌官网为准。"
    )

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
        brand_note = _brand_disclosure(db, resolved, message)
        if brand_note:
            parts.append(brand_note.strip())
        dropped = _dropped_note(db, resolved, message)
        if dropped:
            parts.append(dropped.strip())
        parts.append(footer)
        return "\n".join(parts)

    # 批量核心参数（评审 P2：此前多车系对比逐车系单查，N 次查询）
    facts_by_series = series_fact_rows(db, [s.id for s, _ in resolved])
    heads_map = rank_headlines(facts_by_series)

    # 尺寸**一次批量算完**再分发给各展示点：逐个查会让 N 车系对比多打 2N 条 SQL
    # （审查 P2），而且卡片与逐项对比行必须拿到**同一份**结果。
    size_map = size_lines(db, [series for series, _ in resolved])
    blocks: list[str] = [f"你说的{_count_phrase(len(resolved))}我先放在一起看："]
    for series, brand in resolved:
        name = display_name(series, brand)
        blocks.append(f"\n【{name}】{_series_header(db, series, brand)}")
        head = head_with_size(size_map.get(series.id), heads_map.get(series.id, {}))
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
    # 上面那一块写「5050*1960*1505 mm（6 款有尺寸数据，其中 4 款为此尺寸）」、
    # 下面这行写「4995*1910*1495 mm」——**同一条回答里自相矛盾**。
    heads = [
        head_with_size(size_map.get(series.id), heads_map.get(series.id, {}))
        for series, _ in resolved
    ]
    diff: list[str] = []
    for label in HEADLINE_ORDER:
        values = [h.get(label) for h in heads]
        # 2026-10-05：此前是 `values[0] vs values[1]`——三个及以上车系时
        # **后面的被静默丢掉**（`resolve_series` 上限是 `RESOLVE_SERIES_LIMIT`，当前 6）。
        # 用户问「汉、汉L、秦PLUS 怎么选」，对比行里只有秦PLUS vs 汉，汉L 消失，
        # 而上文三个车系块都在，用户看不出第三个没被比。
        if all(values):
            diff.append(f"{label}：{' vs '.join(values)}")  # type: ignore[arg-type]
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
    if len(resolved) >= 3:
        blocks.append(_summary_for_many(db, resolved))
        brand_note = _brand_disclosure(db, resolved, message)
        if brand_note:
            blocks.append(brand_note)
        dropped = _dropped_note(db, resolved, message)
        if dropped:
            blocks.append(dropped)
        blocks.append("\n" + footer)
        return "\n".join(blocks)

    # 2026-10-07：此前 `resolved[0][0]` / `resolved[1][0]` 在空列表上抛 IndexError。
    # 生产调用点（`engine.py`）有 `if resolved:` 挡着，所以**用户碰不到**；但只要
    # 将来多一个不经那层保护的调用点，或有人拿探针直接调它，就是一个必崩的入口。
    # 这里把崩溃变成确定的空回答，成本一行。
    if not resolved:
        return ""
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
    # 与单车系、≥3 车系两处对齐：**同级/异级都要追加**。
    brand_note = _brand_disclosure(db, resolved, message)
    if brand_note:
        blocks.append(brand_note)
    dropped = _dropped_note(db, resolved, message)
    if dropped:
        blocks.append(dropped)
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
    r"(区别|差别|差异|不同|不一样|对比|比较|哪个好|哪款好|怎么选|选哪|差在哪|差多少|贵在哪|贵多少|值不值|值得吗|"
    # 2026-10-05（端到端扫版本路径发现）：下面这些是**清点型**问法——用户明确在问版本，
    # 但句式里没有「区别/差异/怎么选」这类比较词，于是 HINT 命中而 ASK 落空，
    # 确定性版本路径整个不触发：
    #   「Model Y有哪些版本」「分几个版本」「星愿哪个版本好」「星愿哪款值得买」
    # → 前两类落到 LLM 链路（拿不到版本清单）；后两类走单车系档案路径，
    #   答的是定位/核心参数/亮点，**一个版本都没提**。
    # 只扩 ASK、HINT 不动（仍需「版本/款型/顶配」等显式版本词），
    # 17 条真实问法实测覆盖 9 条中的 7 条、**0 误判**。
    # 「有几种配置/分几个配置」仍不命中（HINT 认「版本」不认「配置」）——
    # 放宽 HINT 会让「星愿配置怎么样」这类想问档案的问法被误判成版本问法，暂不做。
    r"有哪些|有哪几|分几个|几种|值得买|哪个版本)"
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

