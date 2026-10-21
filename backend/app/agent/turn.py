"""一轮对话如何推进会话画像（`respond` 的确定性前半段）。

## 为什么有这个 module

2026-10-20 之前，这些逻辑住在 `agent/engine.py` 的 `respond()` 里，
与 LLM 调用路径交织在一个函数中：

    段一 · 画像合并   载入 → hints → L1 → unknowns → L4 → 落盘①
    闲聊早退
    段二 · 车系锁定   车系解析 → 品牌约束 → 锁定状态机 → 对比跟随 → 落盘②

于是「画像怎么推进」这件事**没有任何 interface**——调用者要知道的不是
「怎么用这个函数」，而是「这 110 行的顺序不能动」。最直接的证据是
engine.py 里那行注释：

> ⚠️ 必须在 set_profile **之前**：放后面的话权重改了却没落盘，下一轮又变回原样，
> 表现为「用户点了没反应」

**顺序正确性只能靠注释和纪律维护，没有 compile-time 保护。**
这正是 locality 缺失：知识被迫以注释形式附着在流程顺序上，而不是封装成 module。

## 两段，边界不同

段一与段二的**执行边界不一样**，这是本 module 最重要的设计约束：

| | 段一（画像合并） | 段二（车系锁定） |
| --- | --- | --- |
| 闲聊时 | **执行** | **不执行** |
| 落盘 | ①（段一后） | ②（段二后） |

**为什么必须保留这个边界**：闲聊早退夹在两段之间。合并成一个函数的话，
闲聊消息里若含车系名（如「你好，汉怎么样」）就会**新增锁定**——
语义上更合理，但那是**行为变更**，需 A/B 与金标回归，不该混在一次重构里。

**落盘由调用方做**，位置与次数逐条保持不变：落盘点依赖早退分支
（闲聊早退在段一后、版本差异早退在段二后），若 module 自己落盘，
早退分支就得知道「谁落了盘」，interface 变宽。

## 依赖方向

`turn` → `soft_prefs` / `soft_probe` / `session` / `profile` / `routing` /
`series_qa` / `catalog`，而**它们都不 import `turn`**——不成环。

本 module 不 import `engine`——那会成环（engine 顶层 import soft_prefs，
而 turn 也 import soft_prefs；turn → engine → turn）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.agent import soft_prefs, soft_probe
from app.agent.feedback import load_rejected_variant_ids
from app.agent.preferences import (
    effective_brand_exclusions,
    persist_hints,
    restore_into_profile,
)
from app.agent.profile import merge_profile
from app.agent.routing import (
    _COMPARE_FOLLOWUP_RE,
    _UNLOCK_RE,
    asks_brand_count,
    asks_brand_lineup,
    asks_catalog_count,
)
from app.agent.schemas import UserProfile
from app.agent.series_qa import NEGATION_RE, negates_series
from app.agent.session import SessionStore
from app.agent.weights import (
    DEFAULT_WEIGHTS,
    USAGE_HINTS,
    WEIGHT_DIM_KEYWORDS,
    WEIGHT_RAISE,
)
from app.catalog.brands import resolve_brand_mentions
from app.catalog.series_constraints import (
    BUDGET_BARE_RE,
    BUDGET_MAX_RE,
    BUDGET_MIN_LOOSE_RE,
    BUDGET_MIN_RE,
    BUDGET_RANGE_BOTH_RE,
    BUDGET_RANGE_RE,
    parse_passengers,
)
from app.catalog.series_index import load_series_pairs, resolve_series
from app.common.models import Brand, VehicleSeries


@dataclass
class TurnProfile:
    """一轮对话推进画像后的产物。

    profile      推进后的画像（**原地修改**的同一个对象）；
    hints        本轮解析出的结构化线索（`extract_hints` 的结果）；
    structured   本轮是否有结构化输入（`bool(hints)` 或品牌线索）。
    resolved     本轮解析出的车系（**车系解析被提到段一**——见 `advance_profile`）；
    brand_hints  本轮解析出的品牌线索（同上；**并入画像**仍在段二）。
    """

    profile: UserProfile
    hints: dict
    structured: bool
    resolved: list = field(default_factory=list)
    brand_hints: dict = field(default_factory=dict)




# 两条匹配路径（偏好 `body` / avoid 排除项）现已**统一用小写化的 msg_low**，
# 因此这张表**只需要小写键**。此前偏好路径用 msg_low、avoid 路径用原始 message，
# 于是不得不同时留「MPV」与「mpv」两个键——当时功能没坏，但看起来像冗余，
# 删掉大写键就会让「不要MPV」静默漏掉排除项（2026-10-03 统一后消除该陷阱）。
_BODY_HINTS = {"轿车": "sedan", "suv": "suv", "mpv": "mpv"}


_ENERGY_HINTS = {
    "纯电": "BEV",
    "插混": "PHEV",
    "插电混动": "PHEV",
    "增程": "EREV",
    "油混": "HEV",
    "混动": "HEV",
    "燃油": "ICE",
    "油车": "ICE",
    "汽油": "ICE",
    "新能源": "new_energy",
}


# 否定词与关键词之间的最大字距（防止跨半句乱指）
_NEGATION_WINDOW = 12

# 旧实现 `if "不要" in message or "不考虑" in message:` 只要句中出现否定词就
# 把句中**所有**车型/能源关键词塞进 avoid，已确认两个缺陷（提案 §4.3）：
#   ① 词表只 2 个词 →「不想开MPV」「不喜欢轿车」落进**偏好**路径，
#      系统推荐用户明确排斥的车；
#   ② 无词边界的子串匹配 →「不要**太贵的**SUV」把 SUV 排除，而 avoid 直接下推
#      SQL `NOT body_type IN (...)`，等于把用户唯一想要的车型从候选集删掉。
_NEGATION_WORDS: tuple[str, ...] = (
    "不要", "不考虑", "不想", "不需要", "不喜欢", "不打算", "讨厌", "排除", "别",
)
# 假朋友：中文不做词边界就会命中「差**别**」「**别**克」。这条不是洁癖——
# 本人审计脚本把语料里 41 处「别」全当否定，报出 7 条「误判」，7 条全是假的。
_NEGATION_FRIENDS: tuple[tuple[str, str], ...] = (("差", "别"), ("别", "克"))


def _negated_values(msg_low: str) -> list[str]:
    """返回被否定修饰的车型/能源值（已去重排序）。

    逐个关键词判定：它**自己所在分句**的前缀里有没有否定词，且否定词与它之间
    没有「的」。「的」是这条规则的关键——「不要太贵的SUV」里否定作用在「贵的」
    上，SUV 是正向需求；「不要MPV」里否定直接管住 MPV。
    """
    out: set[str] = set()
    for table in (_ENERGY_HINTS, _BODY_HINTS):
        for key, value in table.items():
            k = key.lower()
            start = msg_low.find(k)
            while start != -1:
                clause_prefix = _CLAUSE_SPLIT_RE.split(msg_low[:start])[-1]
                window = clause_prefix[-_NEGATION_WINDOW:]
                hit = next(
                    (i for i in range(len(window)) if _negation_here(window, i)),
                    None,
                )
                if hit is not None and _DE not in window[hit:]:
                    out.add(value)
                    break
                start = msg_low.find(k, start + 1)
    return sorted(out)


# 用法关键词 → 用途值、评分维度关键词 → 8 维权重键、单次强调的权重增量：
#
# 2026-10-20：全部搬到 `app/agent/weights.py`（与 DEFAULT_WEIGHTS / WEIGHT_CEILING
# 同属一份知识）。此前它们与 `tools.py` 里的定义**分成两份**，而 `soft_prefs` /
# `soft_probe` 只能**函数内延迟 import** 本模块的私有常量——那是
# `engine ↔ soft_prefs` 循环依赖的疤痕，不是设计。
# 收拢后本模块不再被下游反向依赖，环消失。


_EMPHASIS_RE = re.compile(
    r"(优先|最看重|比较看重|更看重|特别看重|最在意|比较在意|更在意|主要看|重点|看重|在乎|重视|希望)"
)



_DE = "的"


# 分句符：否定作用域不跨这些符号（「不喜欢轿车，SUV可以」里的 SUV 不受影响）
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！!？?\s]")




def _negation_here(text: str, idx: int) -> bool:
    """text[idx:] 是否以一个**真实**的否定词开头（已排除「差别」「别克」）。"""
    for word in _NEGATION_WORDS:
        if not text.startswith(word, idx):
            continue
        if word == "别" and idx > 0 and text[idx - 1] == "差":
            continue  # 「差别」
        if text.startswith("别克", idx):
            continue  # 品牌名
        return True
    return False



def extract_hints(message: str) -> dict:
    """从自然语言中提取可确定的结构化线索（确定性解析，不依赖模型）。"""
    hints: dict = {}
    msg_low = message.lower()

    # 预算解析顺序（评审 P1：此前「20万到30万之间」回落到 bare 把下限当上限）：
    # ① 双侧带万区间 ② 单侧万区间 ③ 上限 ④ 下限 ⑤ 约 N 万起（多万/出头） ⑥ 裸数字=上限
    m = BUDGET_RANGE_BOTH_RE.search(message)
    if m:
        hints["budget"] = {"min": float(m.group(1)) * 10000, "max": float(m.group(2)) * 10000}
    else:
        m = BUDGET_RANGE_RE.search(message)
        if m:
            hints["budget"] = {"min": float(m.group(1)) * 10000, "max": float(m.group(2)) * 10000}
        else:
            m = BUDGET_MAX_RE.search(message)
            if m:
                hints["budget"] = {"max": float(m.group(1)) * 10000}
            else:
                m = BUDGET_MIN_RE.search(message)
                if m:
                    hints["budget"] = {"min": float(m.group(1)) * 10000}
                else:
                    m = BUDGET_MIN_LOOSE_RE.search(message)
                    if m:
                        hints["budget"] = {"min": float(m.group(1)) * 10000}
                    else:
                        m = BUDGET_BARE_RE.search(message)
                        if m:
                            hints["budget"] = {"max": float(m.group(1)) * 10000}  # 单独数字按「不超过」理解

    seats = parse_passengers(message)
    if seats is not None:
        hints["passengers"] = seats

    body = [value for key, value in _BODY_HINTS.items() if key in msg_low]
    if body:
        hints["body_type"] = sorted(set(body))

    energy = []
    for key, value in _ENERGY_HINTS.items():
        if key in msg_low:
            energy.append(value)
    # 「插电混动/插电式混动」同时包含「混动」会双重命中 PHEV+HEV（评审 M3）：
    # 含「插电」时按插混理解，除非用户同时明确提及「油混/双擎/轻混」
    if "插电" in message and "HEV" in energy and not any(k in message for k in ("油混", "双擎", "轻混")):
        energy.remove("HEV")
        if "PHEV" not in energy:
            energy.append("PHEV")
    if energy:
        hints["energy_preference"] = sorted(set(energy))

    usage = [value for key, value in USAGE_HINTS.items() if key in message]
    # 「自己用/个人开」等（匹配短语避免「你自己看着办」误判）
    if re.search(r"(自己用|自己开|自己坐|个人用|个人开|就我|就我自己)", message):
        if "通勤" not in usage:
            usage.append("通勤")
    if usage:
        hints["usage"] = sorted(set(usage))

    if "充电" in message:
        if "不能" in message or "没有" in message or "不方便" in message:
            hints["charging_tolerance"] = False
        else:
            hints["charging_tolerance"] = True

    # 否定句 → avoid（2026-10-03 重写：从「全句级判定」改为**按分句 + 邻近 + 的字边界**）
    #
    # 此前是 `if "不要" in message or "不考虑" in message:`——只要句子里出现否定词，
    # **句中所有**车型/能源关键词一律进 avoid。两个已确认的缺陷（见提案 §4.3）：
    #   ① 词表只有 2 个词：「不想开MPV」「不喜欢轿车」进不了否定分支，
    #      落进**偏好**路径 → 推荐用户明确排斥的车；
    #   ② 无词边界的子串匹配：「不要**太贵的**SUV」里的「不要」否定的不是 SUV，
    #      语义是「要 SUV、但别太贵」——旧实现把 SUV **排除**掉，
    #      而 avoid 直接下推 SQL NOT body_type IN (...)，等于把用户唯一想要的车型删了。
    #
    # 现在逐个关键词判定「它自己前面那一小段里有没有否定」：
    #   - 分句：只在本分句内找否定（「不喜欢轿车，SUV可以」里的 SUV 不受影响）；
    #   - 邻近：否定词与关键词之间不超过 _NEGATION_WINDOW 字；
    #   - 「的」：否定词与关键词之间有「的」→ 否定作用在「的」之前，关键词是**正向需求**；
    #   - 假朋友：中文不做词边界就会把「差别」「别克」当成否定词（本人审计脚本实测踩过：
    #     把语料里 41 处「别」全当否定，报出 7 条「误判」——7 条全是假的）。
    negated = _negated_values(msg_low)
    if negated:
        hints["avoid"] = negated
        # 同词同时命中「偏好」与「避开」时以避开为准（评审 L2）：
        # 如「不要燃油车」→ avoid=[ICE]，不得同时写入 energy_preference=[ICE]。
        # 过滤后为空则删除键：空列表会让 structured=bool(hints) 误判为
        # 「本轮有车型信息」，把纯闲聊（含「不要」字样）错推进追问链路（评审 M-R2）
        for key in ("energy_preference", "body_type"):
            if key in hints:
                kept = [e for e in hints[key] if e not in negated]
                if kept:
                    hints[key] = kept
                else:
                    hints.pop(key)

    # 评分权重线索（评审 M-R10/§17.2「权重来自用户对话」落地）：
    # 含强调词（优先/最看重/主要看…）且句中点到评分维度时，给出该维度的权重加成。
    # 例：「空间优先，动力也要强」→ weights={space:0.30, power:0.30}
    # （默认值 0.10 + 增量 0.2；发绝对值的原因见 WEIGHT_RAISE 处的注释）
    if _EMPHASIS_RE.search(message):
        dims = sorted({dim for kw, dim in WEIGHT_DIM_KEYWORDS if kw in message})
        if dims:
            hints["weights"] = {
                dim: round(DEFAULT_WEIGHTS.get(dim, 0.0) + WEIGHT_RAISE, 3) for dim in dims
            }

    return hints


async def _load_profile(store: SessionStore, session_id: str) -> UserProfile:
    """从 store 取回画像并反序列化；**脏数据自愈**，不把异常抛给调用方。

    2026-10-02（Critical）：原实现是裸的
    `UserProfile(**await run_in_threadpool(store.get_profile, session_id))`。
    任何一次校验失败——部署时改了 UserProfile 的字段类型、Redis 里被半写、
    历史脏值——都会让 `ValidationError` 逃出 `respond`，于是该 session
    **之后每一次请求恒 500 且不自愈**，用户只能靠「新对话」重置。
    也就是说：一次 schema 变更 = 一次线上批量 500。

    自愈策略：**丢弃脏画像、按空画像重新播种**，并记 error 级日志。
    丢的是缓存态（约束可从后续对话重新收集），不是用户资产；相比整条会话
    卡死，这是明显更好的降级——也符合「失败要显式」的诚实性原则。
    """
    raw = await run_in_threadpool(store.get_profile, session_id)
    try:
        return UserProfile(**raw)
    except (ValidationError, TypeError) as err:
        # TypeError 同样要接：raw 根本不是映射（被写成 list / 字符串 / 数字）时，
        # `**raw` 抛的是 TypeError 而非 ValidationError。两者都是「脏数据」，
        # 自愈策略一致。
        logging.getLogger("app.agent.router").error(
            "会话画像反序列化失败，已重建为空画像: session=%s %s: %s",
            session_id, type(err).__name__, err,
        )
        fresh = UserProfile()
        # 重写回 store：否则下一轮还会读到同一份脏数据、每轮都走重建分支
        await run_in_threadpool(store.set_profile, session_id, fresh.model_dump())
        return fresh


async def advance_profile(
    store: SessionStore,
    session_id: str,
    message: str,
    *,
    db: Session | None = None,
    identity: Any | None = None,
) -> TurnProfile:
    """段一：载入画像 → **记忆回填** → 合并本轮线索 → 清理 unknowns → 处理软缺口回答。

    **不落盘**——落盘点依赖早退分支，由调用方在合适的位置做（见 module 文档）。

    顺序**不能动**，理由逐条记在原处：

    1. `soft_prefs.run_if_enabled` 在 `merge_profile` 之后——L1 抽出的软偏好
       通过 `merge_profile` 并入画像，它自己不改画像。
    2. unknowns 清理在 L1 之后——L1 可能补上 usage，届时「已答」状态要同步。
    3. `soft_probe.apply_probe_answer` 在 unknowns 清理之后、**落盘之前**——
       放后面的话权重改了却没落盘，下一轮又变回原样，表现为「用户点了没反应」。

    ## 跨会话记忆（2026-10-21 合流）

    给了 `db` 时，段一额外做三步跨会话记忆的事（`db` 为 None ⇒ 完全不做，
    行为与接入前逐位相同）：

    1. **车系/品牌解析**提前到这里（原本在段二）——因为记忆回填要用**本轮的**
       品牌要求来消解冲突（记忆里可能有「不要比亚迪」，用户这轮说「我要比亚迪」；
       不消解的话旧记忆会把用户明说的正向约束直接删掉 → 推荐结果空掉）。
       ⚠️ 只提前**解析**；品牌 hints 的**并入画像**仍在段二（`structured` 的
       赋值顺序不能变——「我强调过了，只要奔驰」这类只重申约束的消息会掉进
       通用对话分支，2026-09 回归）。
    2. **回填**旧记忆进画像——必须在 `merge_profile(hints)` **之前**：
       用户改了主意时，「这一轮说的」必须压过「上次记的」。
    3. **写入**本轮明说的线索——必须在合并**之后**：写的是**有效值**
       （记忆 ⊕ 本轮），否则「上个月说过的不要 X」会被「这轮说的不要 Y」覆盖。

    这三条以前是 `engine.respond` 里靠注释维护的顺序；现在它们是本 module 的
    **内部实现细节**，调用方不必知道。
    """
    profile = await _load_profile(store, session_id)
    hints = extract_hints(message)

    resolved: list = []
    brand_hints: dict = {}
    brand_exclusions: dict = {}
    if db is not None:
        # 车系/品牌解析**提前**（理由见 docstring 的「跨会话记忆」一节）
        resolved = await run_in_threadpool(resolve_series, db, message)
        brand_hints = await run_in_threadpool(
            resolve_brand_mentions,
            db,
            message,
            series_names=[s.name for s, _brand in resolved],
            assume_constraint=(
                asks_brand_lineup(message)
                or asks_catalog_count(message)
                or asks_brand_count(db, message)
            ),
        )
        # 品牌排除项由 preferences 算好（它要查库解析品牌名 + 消解冲突）
        brand_exclusions = await run_in_threadpool(
            effective_brand_exclusions, db, identity, profile, brand_hints
        )
        # 回填：旧记忆并进画像（必须在合并本轮线索**之前**）
        await run_in_threadpool(
            restore_into_profile,
            db,
            identity,
            profile,
            turn_hints=hints,
            brand_exclusions=brand_exclusions,
        )
        # 拒绝**跨会话**记住（计划 Phase 2b「反馈表」）：这个身份曾经拒绝过的款型
        # 并进 `profile.rejected_variant_ids` ⇒ 推荐链**复用会话内拒绝那套排除逻辑**
        # （工具层零改动 ✓ ⇒ 两种排除的行为必然一致 ✓）。
        if identity is not None:
            remembered = await run_in_threadpool(
                load_rejected_variant_ids, db, identity
            )
            if remembered:
                profile.rejected_variant_ids = list(
                    dict.fromkeys([*profile.rejected_variant_ids, *remembered])
                )

    profile = merge_profile(profile, hints)

    if db is not None:
        # 写入：把本轮**明说**的线索落库（只写 hints = 确定性正则抽自用户原话；
        # 不写模型推断的——推断猜错会静默砍掉整个候选集）。
        # 走线程池：里面有一次 commit（写库要落盘），别占着事件循环。
        await run_in_threadpool(
            persist_hints,
            db,
            identity,
            hints,
            brand_exclusions=brand_exclusions,
            evidence_span=message[:200],
        )
    # L1 软偏好抽取（sales-agent-proposal §3 L1）。默认 AGENT_SOFT_PREF_MODE=off，
    # 此时下面只多一次 env 读取、**不发起任何 LLM 调用**，行为与接入前逐位相同——
    # 提案 §6 第 2 步要求「先只输出结构化、不接入回答」，先离线评测抽取质量。
    # 闸门与回退全在 soft_prefs.run_if_enabled 内部，本行不承载任何逻辑。
    await soft_prefs.run_if_enabled(profile, message)
    # 已补齐的字段从 unknowns 中移除（评审 M1）：unknowns 语义 = 「最近一次追问未答」，
    # 而非永久未知——否则用户后补的字段永远不会再次校验/追问
    for answered in ("budget", "usage", "passengers"):
        if answered == "budget" and (profile.budget.min is not None or profile.budget.max is not None):
            profile.unknowns = [u for u in profile.unknowns if u != "budget"]
        elif answered == "usage" and profile.usage:
            profile.unknowns = [u for u in profile.unknowns if u != "usage"]
        elif answered == "passengers" and profile.passengers is not None:
            profile.unknowns = [u for u in profile.unknowns if u != "passengers"]
    # L4：用户在**回答**软缺口追问（「你更看重哪一点」→「空间」）。
    # 这条路独立于 extract_hints 的强调词路径——后者要求出现「最看重/主要看」
    # 等词，而用户点完选项通常**只回一个词**（「空间」），没有强调词可匹配。
    # 权重值取 weights.WEIGHT_RAISE，与正则路径同一个数。
    #
    # ⚠️ 必须在落盘**之前**：放后面的话权重改了却没落盘，下一轮又变回原样，
    # 表现为「用户点了没反应」。
    soft_probe.apply_probe_answer(profile, message)
    return TurnProfile(
        profile=profile,
        hints=hints,
        structured=bool(hints),
        resolved=resolved,
        brand_hints=brand_hints,
    )


async def advance_series_locks(
    db: Session,
    message: str,
    profile: UserProfile,
    *,
    resolved: list | None = None,
    brand_hints: dict | None = None,
) -> tuple[list[tuple[VehicleSeries, Brand | None]], bool, bool]:
    """段二：车系解析 → 品牌约束并入 → 锁定/解锁状态机 → 对比跟随合并。

    **不落盘**——同段一。

    返回 `(resolved, structured, wants_unlock)`：

    - `resolved`  本轮解析出的车系列表（可能因「对比跟随」而并入会话历史里锁定的车系）
    - `structured`  本轮是否有结构化输入（品牌线索也算）
    - `wants_unlock`  用户是否明确要求「看其他车」——调用方在「锁定车系与本轮硬约束
      冲突」时还要用它判断该不该自动解锁（见 `engine.respond`）

    顺序**不能动**：

    1. 品牌约束必须在车系解析**之后**——「银河星愿怎么样」里的「银河」属于
       车系名，不是品牌约束（2026-09 回归）。
    2. 锁定状态机在品牌并入**之后**——品牌约束会让候选集变空，
       而锁定集合是「用户点名过、需显式解锁才释放」的，两者是不同的东西。
    3. 对比跟随合并在锁定**之后**——它要把「会话历史里锁定的车系」并入本轮
       resolved，所以必须先有锁定集合。

    ## `resolved` / `brand_hints` 可以由调用方传入（2026-10-21 合流）

    跨会话记忆要在**段一**里用到本轮的解析结果（品牌排除项的冲突消解），
    ⇒ 解析提前到段一 ⇒ 段二**复用**同一份结果，不重复查库
    （`resolve_series` 是一次 DB 查询）。
    不传时行为与从前逐位相同（自己解析 ✓）。
    """
    # 品牌硬约束（「只要奔驰」）：需查库把品牌名解析成 brand_id；且必须结合车系解析结果
    # 消歧——「银河星愿怎么样」里的「银河」属于车系名，不是品牌约束（2026-09 回归）。
    if resolved is None:
        resolved = await run_in_threadpool(resolve_series, db, message)
    if brand_hints is None:
        brand_hints = await run_in_threadpool(
            resolve_brand_mentions,
            db,
            message,
            series_names=[s.name for s, _brand in resolved],
            assume_constraint=(
                asks_brand_lineup(message)
                or asks_catalog_count(message)
                or asks_brand_count(db, message)
            ),
        )
    structured = False
    if brand_hints:
        merge_profile(profile, brand_hints)
        # 品牌线索也算「结构化输入」：否则「我强调过了，只要奔驰」这类只重申约束、
        # 不含购车意图词的消息会掉进通用对话分支，由模型凭记忆回答（实测又答成
        # 「奔驰只有三款纯电」）——必须回到确定性链路，用累计画像重出推荐。
        structured = True
    # 2026-10-02（P5.4 / M4）：原先这里有 3 次**连续**的 set_profile
    # （品牌合并后 / 解锁后 / 重新锁定后），写的是同一个 profile 对象——
    # 后一次必然覆盖前一次。生产存储是云 Redis（socket_timeout=3s），
    # 每次都是一次串行往返，最坏情况一轮对话多付 3 次。
    # 合并为**一次无条件写**：放在 987 所在位置之后，三种变更都已被吸收。
    # 注：932 那次保留——`is_chatty` 会在此之后直接 return，那时
    # 品牌/锁定逻辑尚未执行，那次写是唯一一次落盘机会。
    # 评审 m2：否定词**直接指向**已锁定车系（「我不买星愿了，想要15万的燃油车」）
    # 等同明确解锁——否则「锁定车系 ∩ 新硬约束」为空，推荐必然为空。
    # 只按近距离共现判定：「不想要SUV了，看看银河星愿」否定的是车身形式，
    # 不能把用户刚点名的车系解掉。
    wants_unlock = bool(_UNLOCK_RE.search(message))
    locked_now = set(profile.locked_series_ids)
    negated_locked = any(
        series.id in locked_now and negates_series(message, series.name)
        for series, _brand in resolved
    )
    if profile.locked_series_ids and (wants_unlock or negated_locked):
        if wants_unlock:
            profile.locked_series_ids = []
        else:
            # 评审 m18：只解除**被否定**的车系——同句点名的其他锁定车系保留
            # （「我不买星愿了，A10再看看」不得把 A10 一并移出锁定）
            dropped = {
                s.id for s, _brand in resolved if negates_series(message, s.name)
            }
            profile.locked_series_ids = [i for i in profile.locked_series_ids if i not in dropped]
    if resolved:
        # 会话记忆：用户点名的车系持续锁定，直到明确要求「看其他车」；
        # 否定语境（「我不买汉兰达」）不加锁（评审 P2）
        if not wants_unlock and not NEGATION_RE.search(message):
            mentioned = [s.id for s, _ in resolved]
            profile.locked_series_ids = sorted(set(profile.locked_series_ids) | set(mentioned))

    # 2026-10-05（用户拍板）：本轮只点名了一台、另一台**存在于会话历史**时，
    # 把锁定的那台并入 resolved，否则对比意图整个丢失：
    #   第1轮「汉怎么样」  -> 锁定汉，给了档案
    #   第2轮「和汉L比呢」-> 本轮只解析出汉L 1 个车系，系统反问「预算大概多少？」
    # 「汉L和汉哪个好」正常是因为两台都在本轮消息里。
    # 只在**带对比/指代标记**时合并：否则「它的续航够不够」会被误当成两车对比。
    if (
        len(resolved) == 1
        and profile.locked_series_ids
        and _COMPARE_FOLLOWUP_RE.search(message)
    ):
        current = resolved[0][0].id
        missing = [i for i in profile.locked_series_ids if i != current]
        if missing:
            extra = await run_in_threadpool(load_series_pairs, db, missing[:1])
            resolved = resolved + extra
    return resolved, structured, wants_unlock
