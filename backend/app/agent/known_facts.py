"""已推荐款型的「库内事实」前置 + 「有据却否认」守卫。

2026-10-05 生产实测：用户看完推荐卡片后追问「动力」，系统回复

    「收到，您比较关注动力。不过很抱歉，我手头暂时没有捷途旅行者C-DM、
      方程豹钛7这几款20万内车型的动力参数，不能凭空给您说。」

而库里明明有——实测生产库：捷途旅行者C-DM `最大功率(kW)=280`、`最大扭矩(N·m)=610`，
iCAR V27 `最大功率(kW)=335/185`、`最大扭矩(N·m)=505/300`；而且**同一屏卡片的
「匹配分 0.94」就是这些动力维度算出来的**。

根因不是模型乱编，而是**它看不见数据**：用户从没点名过任何车系
（他只是点了个 chip），会话里没有锁定车系 → 检索没有 series 过滤 → 召不回
那几台车的参数 → 模型只能诚实地说「没有」。

这恰恰暴露了本项目一条铁律被绕过：**「说未披露」必须建立在查过的基础上**。
检索返回空 ≠ 库里没有。把「没查到」当成「事实缺失」讲给用户，是对用户的
**反向编造**——比编造数字更隐蔽，因为它看起来正是诚实性原则在生效。

两道防线（缺一不可，见模块末尾的取舍说明）：

1. `build_known_facts` —— 证据前置：直接查库，把这些事实写进上下文。
2. `false_denial_hits` —— 有据禁否认：模型若**仍在有据的维度**上说「没有/未披露」，
   判定违规并交由调用方改写。

为什么两道都要：
- 只做第 2 道 → 模型仍然看不见数据，只能改口说别的假话；守卫把它从「假否定」
  推到「含糊其辞」，诚实性没修好，体验还更差。
- 只做第 1 道 → 上下文一长（车系档案 + 检索证据 + 历史）模型仍可能漏看那一段，
  然后照旧否认。第 2 道是这条长上下文下的**兜底断言**。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import SpecFact, VehicleSeries, VehicleVariant

#: 维度 → (事实键候选, 用户话里代表这个维度的词)。
#: 事实键直接抄 `tools.py` 的评分用键（同一批配置事实，不另造一套口径）。
#: 「座位」刻意**不在**守卫维度里：「没有 7 座版本」是合法陈述，不是「查不到座位数」，
#: 把它算进假否定会让守卫拦下正常回答——守卫宁可漏杀不可错杀。
DIMENSION_FACT_KEYS: dict[str, tuple[str, ...]] = {
    "power": (
        "最大功率(kW)", "电动机总功率(kW)", "发动机最大功率(kW)", "系统综合功率(kW)",
        "最大扭矩(N·m)", "电动机总扭矩(N·m)", "系统综合扭矩(N·m)", "发动机-最大扭矩(N·m)",
        "官方0-100km/h加速(s)", "官方0-100km/h加速时间(s)",
    ),
    "space": ("长度(mm)", "长*宽*高(mm)", "车长(mm)", "轴距(mm)"),
    "range": ("CLTC续航里程(km)", "NEDC续航里程(km)", "WLTC续航里程(km)", "续航里程(km)"),
    "consumption": ("WLTC油耗(L/100km)", "NEDC油耗(L/100km)", "综合油耗(L/100km)"),
}

#: 维度 → 用户话里的触发词（用于**回答侧**校验，不用于查库）
DIMENSION_WORDS: dict[str, tuple[str, ...]] = {
    "power": ("动力", "功率", "扭矩", "马力", "加速", "零百", "推背"),
    "space": ("空间", "轴距", "车长", "尺寸", "后备箱", "行李厢"),
    "range": ("续航", "能跑多远", "跑多远"),
    "consumption": ("油耗", "电耗", "能耗", "费油", "省电"),
}

#: 「这条数据我没有」的措辞。分两类：
#: 1. 定长短语——否认词与宾语固定相邻（「未披露」「查不到」「手头没有」…）。
#: 2. `_BARE_DENIAL_RE`——裸否定，宾语被修饰词隔开（「没有这几款车的动力参数」）。
#:    审查 M1 实测：只靠定长短语会漏判绝大多数**最贴近生产原句**的说法。
_DENIAL_MARKERS = (
    "未披露", "没有披露", "查不到", "拿不到", "无法获取", "获取不到",
    "暂时没有", "暂时无", "暂无数据", "暂无", "没有数据", "没有参数",
    "没有资料", "没有信息", "手头没有", "手上没有", "我这边没有", "这边没有",
    "没有该", "缺乏", "缺失", "不便提供", "不太方便给",
)
#: 裸否定：否认词与「数据类宾语」之间允许夹修饰词，但不许跨小句。
_BARE_DENIAL_RE = re.compile(
    r"(?:没有|无|未有|找不到|查不到|拿不到|拿不出|无法提供|不能提供)"
    r"[^。！？!?；;\n]{0,14}?(?:参数|数据|资料|信息|披露|记录)"
)
#: 数据可得性线索：只有同时出现它，才认定这句在讲「数据有没有」而不是别的否定。
_DATA_CUES = ("数据", "参数", "资料", "信息", "披露", "记录", "数值")
#: 小句切分符。
#: ⚠️ **不切「、」**：中文里「我手头暂时没有捷途旅行者C-DM、方程豹钛7这两款车型的
#: 动力参数」里的顿号分隔的是**宾语的并列项**，切了就把生产原句拆散、漏判。
#: 切「，」则正好把「否认 A，……（顺带说 B）」这类误伤挡掉（审查 H2 实测三例）。
_SENTENCE_SPLIT = re.compile(r"[。！？!?；;，,\n]")

#: 前置给模型的块标题。它必须写明**这是权威事实**，否则模型仍会按「检索片段」对待它。
_KNOWN_FACTS_HEADER = (
    "【已推荐车型的库内参数（直接来自数据库，权威，可直接引用）】\n"
    "下列参数确实存在于库中；被问到这些参数时**只能依据这里作答**，"
    "不得回答「未披露/查不到」。只有这里没写的参数才可以说未披露。"
)


def _norm_keys(keys: tuple[str, ...]) -> list[str]:
    return list(keys)


def build_known_facts(db: Session, variant_ids: list[int], limit: int = 4) -> tuple[str, set[str]]:
    """查库取已推荐款型的参数事实 → (可注入上下文的文本块, 确实有数据的维度集合)。

    第二个返回值是**守卫的判据**：只有落在返回集合里的维度，才允许判「假否定」。
    维度没有数据时，模型说「未披露」是完全正确的，不能拦。
    """
    ids = [int(i) for i in (variant_ids or []) if i][:limit]
    if not ids:
        return "", set()

    rows = db.execute(
        select(VehicleVariant.id, VehicleVariant.display_name, SpecFact.fact_key, SpecFact.fact_value)
        .join(VehicleSeries, VehicleSeries.id == VehicleVariant.series_id, isouter=True)
        .join(SpecFact, SpecFact.variant_id == VehicleVariant.id, isouter=True)
        .where(VehicleVariant.id.in_(ids))
    ).all()

    by_variant: dict[int, tuple[str, dict[str, str]]] = {}
    for vid, name, key, value in rows:
        name, facts = by_variant.setdefault(vid, (name, {}))
        if key and value and key in {k for keys in DIMENSION_FACT_KEYS.values() for k in keys}:
            facts.setdefault(str(key), str(value))

    have_dims: set[str] = set()
    lines: list[str] = []
    for name, facts in by_variant.values():
        if not facts:
            continue
        # 一个事实键可能服务多个维度（如「最大功率(kW)」同时算 power）
        for dim, keys in DIMENSION_FACT_KEYS.items():
            if any(k in facts for k in keys):
                have_dims.add(dim)
        pairs = "；".join(f"{k}={v}" for k, v in facts.items())
        lines.append(f"- {name}：{pairs}")

    if not lines:
        return "", set()
    return _KNOWN_FACTS_HEADER + "\n" + "\n".join(lines), have_dims


def _has_denial(clause: str) -> bool:
    """小句里是否出现「数据可得性否认」（定长短语或裸否定任一）。"""
    if any(d in clause for d in _DENIAL_MARKERS):
        return True
    return _BARE_DENIAL_RE.search(clause) is not None


def false_denial_hits(text: str, have_dims: set[str]) -> list[str]:
    """回答里「在确实有数据的维度上说没有」→ 返回命中的维度键列表。

    判定单位是**小句**（按「，；。！？」切，**不切顿号**），不是固定字符窗口：

    - 窗口法在生产原句上必然失效：「我手头暂时没有捷途旅行者C-DM、方程豹钛7这两款
      20 万内车型的动力参数」里，否认词到维度词隔了 29 个字——调准就脆、调宽就误伤。
    - 整句聚合又会误伤（审查 H2 实测三例）：「这台车没有披露辅助驾驶配置，动力参数
      倒是齐全」里 denying 的是辅助驾驶，power 只是顺带被提到。小句切分正好分开。

    一个小句命中三个信号才算违规：
    1. _has_denial —— 否认数据可得性（定长短语或裸否定）；
    2. 出现 _DATA_CUES 之一 —— 确实在讲数据，不是在讲别的否定；
    3. 出现某个**有据**维度的词（have_dims 里有）。

    小句内出现多个维度词时归给**离否认词最近**的那个：「动力参数都披露了但续航我没有
    数据」要判 range。守卫宁可漏杀不可错杀——错杀会把一条本来正确的回答改掉，
    伤害比放过一次假否定更大。
    """
    if not text or not have_dims:
        return []
    hits: list[str] = []
    for clause in _SENTENCE_SPLIT.split(text):
        if not clause or not _has_denial(clause):
            continue
        if not any(c in clause for c in _DATA_CUES):
            continue
        marker_positions = [i for d in _DENIAL_MARKERS for i in _find_all(clause, d)]
        marker_positions += [m.start() for m in _BARE_DENIAL_RE.finditer(clause)]
        if not marker_positions:
            continue
        # (维度 → 该维度词到最近否认词的距离)
        best: dict[str, int] = {}
        for dim in have_dims:
            for word in DIMENSION_WORDS.get(dim, ()):
                for wpos in _find_all(clause, word):
                    dist = min(abs(wpos - m) for m in marker_positions)
                    if dim not in best or dist < best[dim]:
                        best[dim] = dist
        if not best:
            continue
        nearest = min(best, key=lambda d: best[d])
        if nearest not in hits:
            hits.append(nearest)
    return hits


def render_facts_only(block: str) -> str:
    """从上下文块里剥出**纯事实行**，丢掉块头（块头是给模型看的系统指令）。

    兜底路径要把事实直给用户时必须走这里：直接拼整个块会把
    「只能依据这里作答 / 不得回答未披露」这类指令原文暴露给用户，
    而 `safety_guard` 只拦优惠/库存/成交，拦不住（审查 H2 实测）。
    """
    facts = [ln.strip() for ln in (block or "").splitlines() if ln.strip().startswith("- ")]
    if not facts:
        return ""
    return "以下参数直接来自数据库：\n" + "\n".join(facts)


def _find_all(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    start = haystack.find(needle)
    while start != -1:
        out.append(start)
        start = haystack.find(needle, start + 1)
    return out
