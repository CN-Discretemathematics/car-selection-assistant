"""Agent 引擎。

确定性主链：需求意图解析 → 判断明确性 → 最小化追问 → 结构化画像 →
PostgreSQL 硬条件筛选 → 确定性推荐评分 → 来源与事实校验 → 解释。
DeepSeek 只负责解释生成；车辆事实一律来自工具与数据库（原则 3/17）。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.agent.schemas import (
    AgentMessageOut,
    Budget,
    Citation,
    Clarification,
    RecommendedVariant,
    UserProfile,
)
from app.agent.series_qa import (
    NEGATION_RE,
    asks_variant_diff,
    build_series_qa_answer,
    build_variant_diff_answer,
    negates_series,
    should_answer,
)
from app.agent.session import SessionStore, get_session_store
from app.agent.tools import citation_verifier, recommendation_tool, retrieval_search, safety_guard
from app.catalog.series_index import display_name, resolve_series
from app.common.enums import NEW_ENERGY_TYPES
from app.common.llm import LLMClient, LLMError, get_llm_client
from app.common.models import Brand, OfficialPrice, Source, VehicleSeries, VehicleVariant

_BUDGET_RANGE_BOTH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*[-~到至]\s*(\d+(?:\.\d+)?)\s*万")  # 20万到30万
_BUDGET_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~到至]\s*(\d+(?:\.\d+)?)\s*万")  # 20-30万 / 20到30万
_BUDGET_MAX_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*(?:以内|以下|之内|内)")
_BUDGET_MIN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*(?:以上|起步|起)")
# 「20多万」「30万出头」「将近30万」→ 下限口径（约 N 万起，不给上限）
_BUDGET_MIN_LOOSE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万?\s*(?:多万|出头|大几万|往上)")
_BUDGET_BARE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万")
_PASSENGERS_RE = re.compile(r"([一二两三四五六七八九十\d]+)\s*(?:个|口)?人|([一二两三四五六七八九十\d]+)\s*座")
_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# ── 意图路由（确定性，不依赖模型）──────────────────────────────────────────
# 闲聊/能力咨询：整句匹配才走闲聊路径；带购车内容的句子不受影响
_CHATTY_RE = re.compile(
    r"^(你好|您好|你好呀|hi|hello|嗨|哈喽|在吗|在不在|早上好|中午好|晚上好|早安|晚安|"
    r"谢谢|多谢|感谢|再见|拜拜|你是谁|你叫什么|介绍一下你自己|你能做什么|你能帮我什么|"
    r"能帮我什么|你可以帮我什么|有什么功能|你会什么|会什么|你能干什么|能干什么|能干嘛|"
    r"怎么用|如何使用|怎么玩|帮助|help)[!！。？?\s]*$",
    re.IGNORECASE,
)
# 购车意图：出现即走「真实数据推荐」链路
_CAR_INTENT_RE = re.compile(
    r"(买车|购车|选车|帮我选|帮我挑|推荐|预算|SUV|MPV|轿车|新能源|纯电|插混|增程|"
    r"油混|混动|燃油|油车|汽油|车型|车款|续航|油耗|配置|四驱|两驱|落地价|落地|试驾|"
    r"二手车|空间|价位|哪款|哪台|什么车|[一二三四五六七八九十\d]+[万座]|"
    r"优点|优势|亮点|卖点|缺点|对比|相比|比较|差别|区别|值不值|性价比|怎么样|好不好|值得买|"
    r"选哪|选台|选个|挑台|挑个)",
    re.IGNORECASE,
)
_CAR_BUY_RE = re.compile(r"(?:买|购|选|提|试)[^。！？!?]{0,6}车")
# 通用购车咨询（无画像时也给出有据可查的回答，而不是硬推“没预算的推荐”）
_GENERAL_ADVICE_RE = re.compile(
    r"(哪个好|怎么选|如何选|怎么挑|区别|优缺点|值得买|推荐吗|怎么样|好不好|适合我|"
    r"如何选择|帮我分析|有没有|哪款更|更推荐|优点|优势|亮点|卖点|缺点|值不值|性价比|"
    r"对比|相比|比较|差别|选哪个|选哪款|选什么)"
)
# 用户明确要求「看其他车」：清空已锁定车系，回到广泛推荐。
# 收窄为明确句式（评审 P1：「另外…」「再看…」等话语词不得误清空锁定）
_UNLOCK_RE = re.compile(
    r"(看看其他|看看别的|其他车|别的车|其他选择|别的选择|还有什么选择|更多选择|"
    r"换个|换一换|换别的|不要只看|不只看|都看看|别的车型|其他车型)"
)
# 用户授权「宽松推荐」的口语表达（「没想好/不知道」表示信息缺失，不算授权）
_GIVE_UP_RE = re.compile(r"(随便|无所谓|都可以|你看着办|你推荐|听你的|你来定|相信你)")


def is_chatty(message: str) -> bool:
    return bool(_CHATTY_RE.match(message.strip()))


def has_car_intent(message: str) -> bool:
    return bool(_CAR_INTENT_RE.search(message) or _CAR_BUY_RE.search(message))


def asks_general_advice(message: str) -> bool:
    return bool(_GENERAL_ADVICE_RE.search(message))


def gives_up_on_profile(message: str) -> bool:
    return bool(_GIVE_UP_RE.search(message))


def profile_has_core_constraints(profile: UserProfile) -> bool:
    """核心约束（预算/用途/人数/车身）——能源偏好单独出现（如「电动车和油车哪个好」）
    不足以说明用户已有具体购车需求，通用咨询仍然作答。"""
    return any(
        (
            profile.budget.min is not None or profile.budget.max is not None,
            bool(profile.usage),
            profile.passengers is not None,
            bool(profile.body_type),
        )
    )


# ── 会话锁定车系的冲突检测（用户反馈 P1）──────────────────────────────────────
# 场景：先点名纯电「银河星愿」→ 会话锁定；再说「我想买一台 15 万的燃油车」。
# 旧逻辑只有「看看其他车」这类明确句式才解锁，于是推荐仍被限定在星愿内，
# 与新的能源/预算硬约束叠加后必然为空——用户观感就是「Agent 把参数限定在星愿上」。
# 这里按硬约束探测「锁定车系内是否还有可行 SKU」：为空即视为需求已转移，
# 自动解除锁定并明确告知用户（不静默改变口径）。
_ALL_ENERGY_TYPES = ("BEV", "PHEV", "EREV", "HEV", "ICE")


def _expand_energy_prefs(prefs: list[str]) -> set[str]:
    """能源偏好展开（与 recommendation_tool 同一口径：new_energy/fuel 泛化）。"""
    allowed = {e for e in prefs if e not in ("new_energy", "fuel")}
    if "new_energy" in prefs:
        allowed |= set(NEW_ENERGY_TYPES)
    if "fuel" in prefs:
        allowed |= {t for t in _ALL_ENERGY_TYPES if t not in NEW_ENERGY_TYPES}
    return allowed


def _expand_avoid(avoid: list[str]) -> tuple[set[str], set[str]]:
    """排除偏好展开为 (能源集合, 车身集合)。"""
    avoided = set(avoid or [])
    energy_avoid = {e for e in avoided if e in _ALL_ENERGY_TYPES}
    if "new_energy" in avoided:
        energy_avoid |= set(NEW_ENERGY_TYPES)
    if "fuel" in avoided:
        energy_avoid |= {t for t in _ALL_ENERGY_TYPES if t not in NEW_ENERGY_TYPES}
    body_avoid = {b for b in avoided if b in ("sedan", "suv", "mpv", "pickup")}
    return energy_avoid, body_avoid


def locked_series_conflict(db: Session, profile: UserProfile) -> bool:
    """锁定车系内是否已无任何满足当前硬约束的在售 SKU（True = 冲突，应解锁）。

    SQL 硬约束与 recommendation_tool 逐维一致（在售 / 锁定 / 车身 / 能源含
    new_energy·fuel 泛化 / 排除含泛化 / official_msrp 现价区间），因此
    「探测为空 ⇒ 继续锁定必然推荐为空」成立，不会误伤仍然可行的锁定。
    评审 m1：推荐链在 Python 层还有一条「座位数 ≥ passengers」过滤未纳入探测，
    故反向不严格成立（探测非空仍可能因座位数被筛空）——方向保守，只会漏解锁。
    """
    if not profile.locked_series_ids:
        return False
    stmt = select(VehicleVariant.id).where(
        VehicleVariant.status == "on_sale",
        VehicleVariant.series_id.in_(profile.locked_series_ids),
    )
    if profile.body_type:
        stmt = stmt.where(VehicleVariant.body_type.in_(profile.body_type))
    allowed = _expand_energy_prefs(list(profile.energy_preference or []))
    if allowed:
        stmt = stmt.where(VehicleVariant.energy_type.in_(sorted(allowed)))
    energy_avoid, body_avoid = _expand_avoid(list(profile.avoid or []))
    if energy_avoid:
        stmt = stmt.where(~VehicleVariant.energy_type.in_(sorted(energy_avoid)))
    if body_avoid:
        stmt = stmt.where(~VehicleVariant.body_type.in_(sorted(body_avoid)))
    price_cond = [
        OfficialPrice.effective_to.is_(None),
        OfficialPrice.price_type == "official_msrp",
    ]
    if profile.budget.min is not None:
        price_cond.append(OfficialPrice.price_cny >= profile.budget.min)
    if profile.budget.max is not None:
        price_cond.append(OfficialPrice.price_cny <= profile.budget.max)
    stmt = stmt.where(VehicleVariant.id.in_(select(OfficialPrice.variant_id).where(*price_cond)))
    return db.scalars(stmt.limit(1)).first() is None


def _retrieve_chat_evidence(
    db: Session,
    message: str,
    resolved: list | None,
    locked_series_ids: list[int] | None = None,
) -> list[dict]:
    """闲聊路径的证据检索（LLM 见到的「数据佐证」）。

    优先当前消息解析出的车系（单车系加 series_id 过滤，检索 query 附加规范车系名）；
    当前消息无车系名（如「我看他们价格差不多」的代词指代）时，回退到会话记忆
    锁定的车系——否则上一轮给出的参数/价格在本轮证据中失联，LLM 会按
    「没有数据就如实说不知道」自我否定（评审 M-R9）。
    """
    if resolved:
        names = " ".join(display_name(s, b) for s, b in resolved)
        query = f"{message} {names}".strip()
        filters = {"series_id": resolved[0][0].id} if len(resolved) == 1 else None
        evidence = retrieval_search(db, query, filters, 3)
        if not evidence:
            evidence = retrieval_search(db, query, None, 3)
        return evidence
    if locked_series_ids:
        rows = db.execute(
            select(VehicleSeries, Brand)
            .join(Brand, VehicleSeries.brand_id == Brand.id)
            .where(VehicleSeries.id.in_(locked_series_ids))
        ).all()
        names = " ".join(display_name(s, b) for s, b in rows)
        if names:
            query = f"{message} {names}".strip()
            filters = {"series_id": locked_series_ids[0]} if len(locked_series_ids) == 1 else None
            evidence = retrieval_search(db, query, filters, 3)
            if not evidence:
                evidence = retrieval_search(db, query, None, 3)
            return evidence
    return retrieval_search(db, message, None, 3)


def _parse_count(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    return None
_BODY_HINTS = {"轿车": "sedan", "suv": "suv", "MPV": "mpv", "mpv": "mpv"}
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
_USAGE_HINTS = {
    "上下班": "通勤",
    "通勤": "通勤",
    "日常": "通勤",
    "代步": "通勤",
    "每天": "通勤",
    "平时": "通勤",
    "上班": "通勤",
    "家用": "家庭",
    "家庭": "家庭",
    "带娃": "家庭",
    "接送孩子": "家庭",
    "接送": "家庭",
    "买菜": "家庭",
    "家人": "家庭",
    "长途": "长途",
    "自驾": "长途",
    "旅游": "长途",
    "出差": "商务",
    "商务": "商务",
    "接待": "商务",
    "办公": "商务",
}

# 评分维度关键词 → recommendation_tool 的 8 维权重键（§17.2 软评分）
_WEIGHT_DIM_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("大空间", "space"), ("空间", "space"),
    ("动力", "power"), ("性能", "power"), ("马力", "power"), ("加速", "power"),
    ("续航", "energy"), ("油耗", "energy"), ("能耗", "energy"), ("电耗", "energy"),
    ("智驾", "intelligence"), ("智能", "intelligence"), ("辅助驾驶", "intelligence"),
    ("车机", "intelligence"), ("科技", "intelligence"),
    ("舒适", "comfort"), ("舒服", "comfort"), ("隔音", "comfort"),
    ("保养", "maintenance"), ("维修", "maintenance"), ("省心", "maintenance"), ("售后", "maintenance"),
    ("性价比", "budget"), ("价格", "budget"),
)
_EMPHASIS_RE = re.compile(
    r"(优先|最看重|比较看重|更看重|特别看重|最在意|比较在意|更在意|主要看|重点|看重|在乎|重视|希望)"
)
_WEIGHT_RAISE = 0.2  # 单次强调的权重增量（recommendation_tool 层归一化前叠加）


def extract_hints(message: str) -> dict:
    """从自然语言中提取可确定的结构化线索（确定性解析，不依赖模型）。"""
    hints: dict = {}
    msg_low = message.lower()

    # 预算解析顺序（评审 P1：此前「20万到30万之间」回落到 bare 把下限当上限）：
    # ① 双侧带万区间 ② 单侧万区间 ③ 上限 ④ 下限 ⑤ 约 N 万起（多万/出头） ⑥ 裸数字=上限
    m = _BUDGET_RANGE_BOTH_RE.search(message)
    if m:
        hints["budget"] = {"min": float(m.group(1)) * 10000, "max": float(m.group(2)) * 10000}
    else:
        m = _BUDGET_RANGE_RE.search(message)
        if m:
            hints["budget"] = {"min": float(m.group(1)) * 10000, "max": float(m.group(2)) * 10000}
        else:
            m = _BUDGET_MAX_RE.search(message)
            if m:
                hints["budget"] = {"max": float(m.group(1)) * 10000}
            else:
                m = _BUDGET_MIN_RE.search(message)
                if m:
                    hints["budget"] = {"min": float(m.group(1)) * 10000}
                else:
                    m = _BUDGET_MIN_LOOSE_RE.search(message)
                    if m:
                        hints["budget"] = {"min": float(m.group(1)) * 10000}
                    else:
                        m = _BUDGET_BARE_RE.search(message)
                        if m:
                            hints["budget"] = {"max": float(m.group(1)) * 10000}  # 单独数字按「不超过」理解

    m = _PASSENGERS_RE.search(message)
    if m:
        count = _parse_count(m.group(1) or m.group(2) or "")
        if count is not None:
            hints["passengers"] = count

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

    usage = [value for key, value in _USAGE_HINTS.items() if key in message]
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

    if "不要" in message or "不考虑" in message:
        for key, value in _ENERGY_HINTS.items():
            if key in message:
                hints.setdefault("avoid", []).append(value)
        for key, value in _BODY_HINTS.items():
            if key in message:
                hints.setdefault("avoid", []).append(value)
        # 同词同时命中「偏好」与「避开」时以避开为准（评审 L2）：
        # 如「不要燃油车」→ avoid=[ICE]，不得同时写入 energy_preference=[ICE]。
        # 过滤后为空则删除键：空列表会让 structured=bool(hints) 误判为
        # 「本轮有车型信息」，把纯闲聊（含「不要」字样）错推进追问链路（评审 M-R2）
        avoided = set(hints.get("avoid", []))
        for key in ("energy_preference", "body_type"):
            if key in hints:
                kept = [e for e in hints[key] if e not in avoided]
                if kept:
                    hints[key] = kept
                else:
                    hints.pop(key)

    # 评分权重线索（评审 M-R10/§17.2「权重来自用户对话」落地）：
    # 含强调词（优先/最看重/主要看…）且句中点到评分维度时，给出该维度的权重加成。
    # 例：「空间优先，动力也要强」→ weights={space:+0.2, power:+0.2}
    if _EMPHASIS_RE.search(message):
        dims = sorted({dim for kw, dim in _WEIGHT_DIM_KEYWORDS if kw in message})
        if dims:
            hints["weights"] = {dim: _WEIGHT_RAISE for dim in dims}

    return hints


# 「还差一项」引导：缺失字段 → (问题, 示例选项)
_MISSING_ASK: dict[str, tuple[str, list[str]]] = {
    "budget": ("购车预算大概是多少？", ["10万以内", "10~20万", "20~30万", "30万以上"]),
    "usage": ("主要用途是什么？", ["上下班通勤", "家庭出行", "长途自驾", "商务接待"]),
    "passengers": ("平时一般几个人乘坐？", ["1~2人", "3~5人", "5人以上"]),
}


def _profile_summary(profile: UserProfile) -> str:
    """已收集画像的一句话复述（用于「已记住：…」引导）。"""
    parts: list[str] = []
    if profile.budget.min is not None and profile.budget.max is not None:
        parts.append(f"预算 {profile.budget.min / 10000:g}-{profile.budget.max / 10000:g} 万元")
    elif profile.budget.max is not None:
        parts.append(f"预算不超过 {profile.budget.max / 10000:g} 万元")
    elif profile.budget.min is not None:
        parts.append(f"预算 {profile.budget.min / 10000:g} 万元以上")
    if profile.usage:
        parts.append(f"用途：{'、'.join(profile.usage)}")
    if profile.passengers is not None:
        parts.append(f"{profile.passengers} 人乘坐")
    if profile.body_type:
        parts.append(f"车身：{'、'.join(profile.body_type)}")
    if profile.energy_preference:
        parts.append(f"能源：{'/'.join(profile.energy_preference)}")
    return "；".join(parts)


def merge_profile(profile: UserProfile, hints: dict) -> UserProfile:
    if hints.get("budget"):
        profile.budget = Budget(**hints["budget"])
    if hints.get("passengers") is not None:
        profile.passengers = hints["passengers"]
    if hints.get("body_type"):
        profile.body_type = hints["body_type"]
    if hints.get("energy_preference"):
        profile.energy_preference = hints["energy_preference"]
    if hints.get("usage"):
        profile.usage = hints["usage"]
    if hints.get("charging_tolerance") is not None:
        profile.charging_tolerance = hints["charging_tolerance"]
    if hints.get("avoid"):
        profile.avoid = sorted(set(profile.avoid) | set(hints["avoid"]))
    # 权重线索跨轮累加（如第一轮「空间优先」+0.2，后来说「再看重动力」动力也 +0.2）
    if hints.get("weights"):
        for dim, w in hints["weights"].items():
            profile.weights[dim] = round(profile.weights.get(dim, 0.0) + float(w), 3)
    return profile


def next_clarification(profile: UserProfile) -> Clarification | None:
    """最小化追问策略：一次只问一个关键问题（12.2/13）。"""
    if profile.budget.min is None and profile.budget.max is None:
        return Clarification(
            question="为了帮你挑到合适的车，先问一下：购车预算大概是多少？",
            options=["10万以内", "10~20万", "20~30万", "30万以上"],
            missing=["budget"],
        )
    if not profile.usage:
        return Clarification(
            question="这辆车的主要用途是什么呢？",
            options=["上下班通勤", "家庭出行", "长途自驾", "商务接待"],
            missing=["usage"],
        )
    if profile.passengers is None:
        return Clarification(
            question="平时一般几个人乘坐？",
            options=["1~2人", "3~5人", "5人以上"],
            missing=["passengers"],
        )
    return None


class AgentEngine:
    def __init__(self, llm: LLMClient | None = None, store: SessionStore | None = None) -> None:
        self._llm = llm or get_llm_client()
        self._store = store or get_session_store()

    async def _emit(self, session_id: str, text: str, out: AgentMessageOut) -> None:
        """落库 assistant 消息与结构化结果。

        会话存储生产态是云 Redis（socket_timeout=3s），同步调用会阻塞事件循环，
        统一放线程池执行（评审 M-R1；进程内存储时开销可忽略）。
        """
        await run_in_threadpool(self._store.append_message, session_id, "assistant", text)
        await run_in_threadpool(self._store.set_last_result, session_id, out.model_dump())

    async def handle(self, db: Session, session_id: str, message: str) -> AgentMessageOut:
        await run_in_threadpool(self._store.append_message, session_id, "user", message)

        profile = UserProfile(**await run_in_threadpool(self._store.get_profile, session_id))
        hints = extract_hints(message)
        profile = merge_profile(profile, hints)
        # 已补齐的字段从 unknowns 中移除（评审 M1）：unknowns 语义 = 「最近一次追问未答」，
        # 而非永久未知——否则用户后补的字段永远不会再次校验/追问
        for answered in ("budget", "usage", "passengers"):
            if answered == "budget" and (profile.budget.min is not None or profile.budget.max is not None):
                profile.unknowns = [u for u in profile.unknowns if u != "budget"]
            elif answered == "usage" and profile.usage:
                profile.unknowns = [u for u in profile.unknowns if u != "usage"]
            elif answered == "passengers" and profile.passengers is not None:
                profile.unknowns = [u for u in profile.unknowns if u != "passengers"]
        await run_in_threadpool(self._store.set_profile, session_id, profile.model_dump())
        structured = bool(hints)

        # 0) 闲聊/能力咨询 → 自然回复（大模型 + 检索佐证）
        if is_chatty(message):
            return await self._plain_chat_reply(
                db, session_id, message, locked_series_ids=profile.locked_series_ids
            )

        # 0.5) 车系识别与锁定（必须在「无购车意图 → 普通对话」gate 之前：
        #       「星愿和零跑A10选哪个」不含购车关键词，但提到具体车系，必须走确定性链路）
        wants_unlock = bool(_UNLOCK_RE.search(message))
        resolved = await run_in_threadpool(resolve_series, db, message)
        # 评审 m2：否定词**直接指向**已锁定车系（「我不买星愿了，想要15万的燃油车」）
        # 等同明确解锁——否则「锁定车系 ∩ 新硬约束」为空，推荐必然为空。
        # 只按近距离共现判定：「不想要SUV了，看看银河星愿」否定的是车身形式，
        # 不能把用户刚点名的车系解掉。
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
            await run_in_threadpool(self._store.set_profile, session_id, profile.model_dump())
        if resolved:
            # 会话记忆：用户点名的车系持续锁定，直到明确要求「看其他车」；
            # 否定语境（「我不买汉兰达」）不加锁（评审 P2）
            if not wants_unlock and not NEGATION_RE.search(message):
                mentioned = [s.id for s, _ in resolved]
                profile.locked_series_ids = sorted(set(profile.locked_series_ids) | set(mentioned))
                await run_in_threadpool(self._store.set_profile, session_id, profile.model_dump())

        # 0.55) 同车系「版本 / 款型差异」提问 → 确定性版本级对比（用户反馈 P1）。
        #       必须排在车系档案问答之前：档案用的是车系级聚合事实（同键跨款归并成一行），
        #       版本之间的差异被抹平，用户会得到「没有数据」式的回答。
        #       两种说法都覆盖：句中带车系名（「星愿各版本有什么区别」）、
        #       指代会话锁定车系（选中星愿后只问「不同版本的差异」）。
        if asks_variant_diff(message) and not NEGATION_RE.search(message):
            target: tuple[VehicleSeries, Brand | None] | None = None
            if len(resolved) == 1:
                target = resolved[0]
            elif not resolved and len(profile.locked_series_ids) == 1:
                locked_series = db.get(VehicleSeries, profile.locked_series_ids[0])
                if locked_series is not None:
                    target = (locked_series, db.get(Brand, locked_series.brand_id))
            if target is not None:
                return await self._variant_diff_reply(db, session_id, target[0], target[1], message)

        if resolved and should_answer(resolved, message):
            return await self._series_qa_reply(db, session_id, message, resolved)

        # 1) 无购车意图的普通对话（如「今天天气不错」「帮我算个题」）→ 自然回复
        if not (has_car_intent(message) or structured):
            return await self._plain_chat_reply(
                db, session_id, message, resolved=resolved, locked_series_ids=profile.locked_series_ids
            )

        # 2) 通用购车咨询但还没有核心画像（「电动车和油车哪个好」）→ 有据可查的回答，
        #    不硬推「没有预算的推荐」
        if asks_general_advice(message) and not profile_has_core_constraints(profile):
            return await self._plain_chat_reply(
                db, session_id, message, resolved=resolved, locked_series_ids=profile.locked_series_ids
            )

        # 4) 结构化画像链路：最小化追问 → 硬筛选 → 软评分
        clarification = next_clarification(profile)
        if clarification is not None:
            missing = clarification.missing[0]
            if missing in profile.unknowns:
                # 「授权宽松推荐」必须已有核心画像（预算/用途/人数/车身）；
                # 仅能源偏好或 avoid 不足以授权，避免空画像硬推（评审 S1）
                if gives_up_on_profile(message) and profile_has_core_constraints(profile):
                    clarification = None  # 用户授权「宽松推荐」（unknowns 已含 missing，不重复追加）
                elif structured:
                    # 用户本轮提供了可解析的新信息（如「预算12万，2个人」），但还缺一项：
                    # 复述已收集内容 + 只问缺的那一项（确定性引导，避免「慢慢想就好」乱回）
                    # （missing 已在 unknowns 中，无需再追加）
                    await run_in_threadpool(self._store.set_profile, session_id, profile.model_dump())
                    return await self._targeted_clarify_reply(session_id, profile, missing)
                else:
                    # 追问过仍无信息：用自然语言温和引导，不再硬推无预算的推荐
                    await run_in_threadpool(self._store.set_profile, session_id, profile.model_dump())
                    return await self._plain_chat_reply(
                        db, session_id, message, topic="clarify_guidance", missing=missing,
                        locked_series_ids=profile.locked_series_ids,
                    )
            else:
                profile.unknowns.append(missing)
                await run_in_threadpool(self._store.set_profile, session_id, profile.model_dump())
                # 追问也要落会话历史与 last_result（评审 P2：此前缺失，SSE 回放拿不到追问）
                clarify_out = AgentMessageOut(
                    session_id=session_id,
                    need_clarification=True,
                    clarification=clarification,
                )
                await self._emit(session_id, clarification.question, clarify_out)
                return clarify_out

        # 4.5) 本轮新硬约束与锁定车系冲突 → 自动解除锁定并明确告知（用户反馈 P1）
        #      评审 M1：必须放在追问分支之后。此前放在 0.6 步，若本轮以追问/普通对话
        #      返回，解锁已持久化但说明被丢弃，用户看不到口径变化（下一轮直接出全市场
        #      推荐）。放在这里保证「解锁」与「告知解锁」永远发生在同一次回复里。
        #      评审 m3：探测走线程池，避免云 RDS 下阻塞事件循环。
        unlock_note: str | None = None
        # 判定基准是「已累积画像里的硬约束」而非「本轮提示词」：多轮场景下
        # （先说「15万的燃油车」被追问用途/人数，下一轮只补「家用2个人」）本轮没有
        # 新的硬约束词，但画像已有 ICE + 预算，同样必须探测，否则会拿
        # 「锁定车系 ∩ ICE = 空」去推荐（用户看到的正是最初报的 bug）。
        profile_hard = bool(
            profile.energy_preference
            or profile.body_type
            or profile.avoid
            or profile.budget.min is not None
            or profile.budget.max is not None
        )
        if (
            profile.locked_series_ids
            # 本轮点名了车系 → 用户意图明确，不做自动解锁（避免「看看星愿」被立刻解掉）
            and not resolved
            and profile_hard
            and not wants_unlock
            and not NEGATION_RE.search(message)
            and await run_in_threadpool(locked_series_conflict, db, profile)
        ):
            unlock_note = self._unlock_note(db, profile.locked_series_ids)
            profile.locked_series_ids = []
            await run_in_threadpool(self._store.set_profile, session_id, profile.model_dump())

        # 重计算（批量 SQL）放线程池：避免单 worker 事件循环被大数据量查询阻塞
        result = await run_in_threadpool(recommendation_tool, db, profile)

        filters = {
            "budget_min": profile.budget.min,
            "budget_max": profile.budget.max,
            "body_type": profile.body_type,
            "energy_preference": profile.energy_preference,
            "passengers": profile.passengers,
            # 会话锁定的车系（用户点名过、尚未解锁）——对外暴露便于前端/调试确认口径
            "locked_series_ids": profile.locked_series_ids,
        }

        top = result["variants"]
        citations: list[Citation] = []
        source_names: list[str] = []
        source_ids = sorted({v["source_id"] for v in top if v.get("source_id")})
        if source_ids:
            sources = db.scalars(select(Source).where(Source.id.in_(source_ids))).all()
            name_by_id = {s.id: s.name for s in sources}
            for v in top:
                sid = v.get("source_id")
                citations.append(
                    Citation(
                        source_id=sid,
                        source_name=name_by_id.get(sid) if sid else None,
                        label=f"{v['series_name']} {v['display_name']} 配置与价格",
                    )
                )
                if sid and name_by_id.get(sid) and name_by_id[sid] not in source_names:
                    source_names.append(name_by_id[sid])

        # 来源与事实校验：所有推荐必须可追溯到证据（校验失败时丢弃无来源推荐；评审 L5：
        # 无 source_id 的候选直接剔除，严格落实「任何推荐必须带来源」）
        ok, problems = citation_verifier(
            [{"label": c.label, "source_id": c.source_id} for c in citations],
            [{"source_id": s_id} for s_id in source_ids],
        )
        if not ok and problems:
            citations = [c for c in citations if c.source_id is not None]
        top = [v for v in top if v.get("source_id") is not None]
        # 剔除无来源候选后同步 result，避免 reasons 的数量文案失真（评审 P2）
        result = {**result, "variants": top}

        reasons = self._build_reasons(profile, result)
        if unlock_note:
            # 口径变化必须对用户可见：先说明「已不再限定在某车系」，再给推荐理由
            reasons.insert(0, unlock_note)
        # 内部说明类「妥协项」（未参与评分/暂无数据源等）不呈现给用户（评审：不应出现）
        internal_notes = ("未参与", "暂无", "数据源", "未披露")
        tradeoffs: list[str] = []
        seen: set[str] = set()
        for v in top:
            for t in v["tradeoffs"] or []:
                if any(note in t for note in internal_notes):
                    continue
                if t not in seen:
                    seen.add(t)
                    tradeoffs.append(t)
                if len(tradeoffs) >= 4:
                    break

        # 混合检索证据（§13：官方资料片段作为解释佐证；检索不可用不影响推荐）
        evidence = await run_in_threadpool(self._collect_evidence, db, profile, top)

        explanation = await self._explain(profile, result, source_names, evidence)

        official_links = list(dict.fromkeys(v["official_page_url"] for v in top if v.get("official_page_url")))

        recommended = [
            RecommendedVariant(
                variant_id=v["variant_id"],
                series_id=v["series_id"],
                series_name=v["series_name"],
                brand_name=v["brand_name"],
                display_name=v["display_name"],
                energy_type=v["energy_type"],
                price_cny=v["price_cny"],
                score=v["score"],
                matched=v["matched"],
                tradeoffs=[t for t in (v["tradeoffs"] or []) if not any(n in t for n in internal_notes)],
                official_page_url=v["official_page_url"],
            )
            for v in top
        ]

        out = AgentMessageOut(
            session_id=session_id,
            need_clarification=False,
            filters=filters,
            recommended_series_ids=list(dict.fromkeys(v["series_id"] for v in top)),
            recommended_variants=recommended,
            reasons=reasons,
            tradeoffs=tradeoffs,
            citations=citations,
            official_links=official_links,
            explanation=explanation,
        )
        await self._emit(session_id, explanation or "", out)
        return out

    async def _plain_chat_reply(self, db: Session, session_id: str, message: str,
                                topic: str = "", missing: str = "", resolved: list | None = None,
                                locked_series_ids: list[int] | None = None) -> AgentMessageOut:
        """自然对话回复（无推荐卡片）：LLM+检索佐证；无 LLM 时按话题回退模板。"""
        reply = await self._chat_reply(
            db, session_id, message, topic=topic, missing=missing,
            resolved=resolved, locked_series_ids=locked_series_ids,
        )
        out = AgentMessageOut(
            session_id=session_id,
            need_clarification=False,
            explanation=reply,
        )
        await self._emit(session_id, reply, out)
        return out

    async def _targeted_clarify_reply(self, session_id: str, profile: UserProfile, missing: str) -> AgentMessageOut:
        """用户已给部分可解析信息、还差一项时的确定性引导：
        先复述已记住的内容，再只问缺的那一项（含示例选项），避免「慢慢想就好」式的乱回。"""
        question, options = _MISSING_ASK.get(
            missing, ("还差一些购车信息，请补充一下。", [])
        )
        collected = _profile_summary(profile)
        if collected:
            text = f"已记住：{collected}。还差一项：{question}\n（回复其一即可：{' / '.join(options)}）"
        else:
            text = f"还差一项：{question}（{' / '.join(options)}）"
        out = AgentMessageOut(
            session_id=session_id,
            need_clarification=False,
            explanation=text,
        )
        await self._emit(session_id, text, out)
        return out

    @staticmethod
    def _unlock_note(db: Session, locked_ids: list[int]) -> str:
        """解锁说明（点名车系），避免用户不明白推荐范围为什么变了。"""
        names: list[str] = []
        series_rows = db.scalars(select(VehicleSeries).where(VehicleSeries.id.in_(locked_ids))).all()
        for series in series_rows:
            names.append(display_name(series, db.get(Brand, series.brand_id)))
        target = "、".join(n for n in names if n) or "先前点名的车系"
        return f"已解除对「{target}」的限定：它与本轮新需求冲突，改为在全市场在售车型中筛选。"

    async def _variant_diff_reply(
        self,
        db: Session,
        session_id: str,
        series: VehicleSeries,
        brand: Brand | None,
        message: str,
    ) -> AgentMessageOut:
        """同车系「版本差异」回答：确定性文本 + 各版本候选卡片（可直接加入对比）。"""
        text, rows = await run_in_threadpool(build_variant_diff_answer, db, series, brand, message)
        citations: list[Citation] = []
        source_ids = sorted({r["source_id"] for r in rows if r.get("source_id")})
        if source_ids:
            sources = db.scalars(select(Source).where(Source.id.in_(source_ids))).all()
            name_by_id = {s.id: s.name for s in sources}
            for r in rows:
                sid = r.get("source_id")
                if sid is None:
                    # 评审 m12：无来源的行不生成引用，与推荐链 L5「任何推荐必须带来源」一致
                    continue
                citations.append(
                    Citation(
                        source_id=sid,
                        source_name=name_by_id.get(sid),
                        label=f"{series.name} {r['display_name']} 配置与价格",
                    )
                )
        # score=0 表示「非评分场景」（版本罗列而非推荐），前端据此隐藏匹配分
        recommended = [
            RecommendedVariant(
                variant_id=r["variant_id"],
                series_id=r["series_id"],
                series_name=r["series_name"],
                brand_name=r["brand_name"],
                display_name=r["display_name"],
                energy_type=r["energy_type"],
                price_cny=r["price_cny"],
                score=0.0,
                matched=["同车系版本对比"],
                tradeoffs=[],
                official_page_url=r.get("official_page_url"),
            )
            for r in rows
        ]
        out = AgentMessageOut(
            session_id=session_id,
            need_clarification=False,
            filters={"series_id": series.id},
            recommended_series_ids=[series.id] if rows else [],
            recommended_variants=recommended,
            reasons=[f"按数据库在售 SKU 列出 {series.name} 各版本官方指导价与差异项"],
            citations=citations,
            official_links=[series.official_page_url] if series.official_page_url else [],
            explanation=text,
        )
        await self._emit(session_id, text or "", out)
        return out

    async def _series_qa_reply(
        self,
        db: Session,
        session_id: str,
        message: str,
        resolved: list,
    ) -> AgentMessageOut:
        """具体车系问答：确定性回答（全部来自库内真实参数），不依赖 LLM。"""
        answer = build_series_qa_answer(db, resolved, message)
        source_ids = sorted({s.source_id for s, _ in resolved if s.source_id})
        name_by_id: dict[int, str] = {}
        if source_ids:
            sources = db.scalars(select(Source).where(Source.id.in_(source_ids))).all()
            name_by_id = {s.id: s.name for s in sources}

        citations: list[Citation] = [
            Citation(
                source_id=series.source_id,
                source_name=name_by_id.get(series.source_id) if series.source_id else None,
                label=f"{display_name(series, brand)} 参数与官方指导价",
            )
            for series, brand in resolved
        ]
        out = AgentMessageOut(
            session_id=session_id,
            need_clarification=False,
            explanation=answer,
            citations=citations,
            recommended_series_ids=[s.id for s, _ in resolved],
            official_links=list(
                dict.fromkeys(s.official_page_url for s, _ in resolved if s.official_page_url)
            ),
        )
        await self._emit(session_id, answer, out)
        return out

    async def _chat_reply(self, db: Session, session_id: str, message: str,
                          topic: str = "", missing: str = "",
                          resolved: list | None = None,
                          locked_series_ids: list[int] | None = None) -> str:
        """自然回复：LLM 生成；无 LLM 时按话题回退模板（原则 7）。

        RAG 优化（a）：检索 query 用「消息 + 解析出的车系名」，解析到单车系时按
        series_id 过滤召回，避免整句含寒暄/指代的杂讯 query 召回到无关车型。
        优化（b，评审 M-R9）：当前消息未提及车系时回退到会话锁定车系，
        保证指代性消息（「我看他们价格差不多」）仍能拿到上一轮车型的证据。
        """
        if self._llm.available:
            try:
                if resolved is None:
                    resolved = await run_in_threadpool(resolve_series, db, message)
                evidence = await run_in_threadpool(
                    _retrieve_chat_evidence, db, message, resolved, locked_series_ids
                )
            except Exception:  # noqa: BLE001 - 检索不可用不影响闲聊
                evidence = []
            context = "\n".join(f"- {e['text'][:120]}" for e in evidence[:3])
            # 解析到具体车系时，追加确定性「车系档案」（直接来自 DB 事实，不依赖检索排序）：
            # 否则闲聊路径 top-3 证据可能只是基础信息（定位/长宽），LLM 会说「价格/配置无可靠数据」
            if resolved:
                try:
                    archive = await run_in_threadpool(
                        build_series_qa_answer, db, resolved, message
                    )
                except Exception:  # noqa: BLE001 - 档案生成失败不影响闲聊
                    archive = ""
                clean = "\n".join(
                    ln for ln in str(archive).splitlines()
                    if ln.strip() and not ln.startswith("以上基于")
                )
                if clean:
                    clean = clean[:1400] + ("…" if len(clean) > 1400 else "")
                    context = f"{context}\n【车系档案（数据库）】\n{clean}".strip()
            if topic == "clarify_guidance":
                system = (
                    "你是「选车助手」的智能客服。用户对某类购车信息还不确定，请用温和、简短的话引导。\n"
                    "规则：如果用户本轮已经给出了预算/用途/人数中的任何一项，先确认收到（一句话），"
                    "再只追问缺失的一项并给出示例选项；只有用户表示「没想好/不知道」时才用"
                    "「没关系，慢慢想就好」这类安抚语，并建议先去看看销量榜。\n"
                    f"用户暂时缺失：{missing if missing else '购车信息'}。\n"
                    "示例（用户已给部分信息）：「收到，预算 8 万记下了～剩下的主要用途是"
                    "通勤、家庭还是长途呢？直接回复其一即可。」"
                )
            else:
                system = (
                    "你是「选车助手」的智能客服，帮助用户选购家用新车。要求：\n"
                    "1) 语气自然、友好、简洁，像真人顾问，先回应对方的话；\n"
                    "2) 你能做：按预算/用途/人数/能源偏好推荐真实在售车型、介绍车型配置与官方指导价、回答汽车选购常识；\n"
                    "3) 只依据「数据佐证」和真实数据回答，绝不编造价格、销量或配置；没有数据就如实说不知道；\n"
                    "   涉及具体车型的续航/动力/空间等数字时，只引用「数据佐证」中出现的，一个数字都不能虚构；\n"
                    "4) 用户已给出预算/用途/人数中的任何一项时：先确认收到，再只追问缺失的一项；不要重复问已给的；\n"
                    "5) 不谈论优惠、库存、成交价、贷款，不提供任何购买链接。\n"
                    + (f"数据佐证：\n{context}" if context else "数据佐证：（暂无，聊到具体车型时会检索真实数据）")
                )
            try:
                history = await run_in_threadpool(self._store.history, session_id)
                msgs: list[dict] = [{"role": "system", "content": system}]
                msgs.extend({"role": m["role"], "content": m["content"]} for m in history[:-1])
                msgs.append({"role": "user", "content": message})
                resp = await self._llm.chat(msgs, temperature=0.6)
                text = (resp["choices"][0]["message"]["content"] or "").strip()
                ok, _ = safety_guard(text)
                if ok and text:
                    return text
            except (LLMError, KeyError, TypeError, IndexError, ValueError):
                pass  # LLM 不可用/响应结构异常：回退模板，Agent 仍可用（评审 P2）
        return self._fallback_chat_reply(message, topic=topic)

    @staticmethod
    def _fallback_chat_reply(message: str, topic: str = "") -> str:
        if topic == "clarify_guidance":
            return (
                "没关系，慢慢想就好～这些信息只是为了让推荐更精准，没有也可以。"
                "等你有了大概想法（比如预算十几万、平时主要通勤），我再帮你筛；"
                "也可以先去销量榜看看热门车型。"
            )
        m = message.strip().lower()
        if any(k in m for k in ("你好", "您好", "hi", "hello", "嗨", "哈喽", "在吗", "早上好", "中午好", "晚上好", "早安")):
            return (
                "你好呀！我是选车助手，可以按预算、用途和人数帮你推荐真实在售车型，"
                "也能介绍具体车型的配置和官方指导价。想看看什么类型的车？"
            )
        if any(k in m for k in ("谢谢", "多谢", "感谢")):
            return "不客气！还需要我帮你找车或对比车型吗？"
        if any(k in m for k in ("你是谁", "能做什么", "能帮我什么", "帮我什么", "有什么功能", "你会什么", "会什么",
                                "能干什么", "能干嘛", "怎么用", "如何使用", "怎么玩", "帮助", "help")):
            return (
                "我是选车助手，能帮你：① 按预算/用途/人数/能源偏好推荐真实在售车型；"
                "② 查看销量榜和车型详情；③ 对比不同 SKU 的配置；④ 解答购车常识（比如燃油和纯电怎么选）。"
                "比如告诉我「预算15万，家用5口人，想要新能源SUV」，我就能开始。"
            )
        if any(k in m for k in ("再见", "拜拜", "晚安")):
            return "再见！有购车问题随时来找我。"
        if asks_general_advice(m) or any(k in m for k in ("哪个好", "怎么选", "区别", "优缺点", "怎么选")):
            return (
                "这个问题可以从几个角度看：① 使用场景（通勤/家庭/长途）；② 预算区间；"
                "③ 能源类型（纯电/插混/增程/燃油各有取舍）。"
                "如果已经有具体纠结的车型，直接告诉我车名即可，例如「腾势Z9GT和卡罗拉锐放相比有什么优点」，"
                "我可以基于真实参数（指导价/动力/续航/配置）帮你逐项对比；"
                "或者给我大致预算和用途，我结合真实在售车型帮你分析。"
            )
        return (
            "可以告诉我你的预算、主要用途和乘坐人数吗？比如「预算15万，家用5口人，想要新能源SUV」，"
            "我就能从真实在售车型里帮你挑选。"
        )

    def _collect_evidence(self, db: Session, profile: UserProfile, top: list[dict]) -> list[dict]:
        """为前 2 个候选车系检索官方资料片段（§13 混合检索环节）。"""
        query_parts = list(profile.usage) + list(profile.must_have) + list(profile.nice_to_have)
        if not query_parts or not top:
            return []
        query = " ".join(query_parts)
        evidence: list[dict] = []
        seen_series: set[int] = set()
        for v in top[:2]:
            sid = v["series_id"]
            if sid in seen_series:
                continue
            seen_series.add(sid)
            # 检索词 = 用户需求词 + 候选车系名（系列介绍切片必含车系名，保证可命中）
            series_query = f"{query} {v['series_name']}".strip()
            try:
                hits = retrieval_search(db, series_query, filters={"series_id": sid}, top_k=1)
            except Exception:  # noqa: BLE001 - 检索不可用不影响推荐（原则 7）
                continue
            for hit in hits:
                evidence.append(
                    {"text": hit["text"], "kind": hit["kind"], "source_url": hit["source_url"]}
                )
        return evidence[:2]

    def _build_reasons(self, profile: UserProfile, result: dict) -> list[str]:
        reasons: list[str] = []
        if profile.budget.max is not None:
            reasons.append(f"预算不超过 {profile.budget.max / 10000:g} 万元")
        if profile.energy_preference:
            reasons.append(f"能源偏好：{'/'.join(profile.energy_preference)}")
        if profile.body_type:
            reasons.append(f"车身类型：{'/'.join(profile.body_type)}")
        if profile.usage:
            reasons.append(f"用途：{'/'.join(profile.usage)}")
        reasons.append(
            f"共 {result['count']} 个在售 SKU 满足硬条件，展示带来源的前 {len(result['variants'])} 名"
        )
        return reasons

    async def _explain(
        self,
        profile: UserProfile,
        result: dict,
        source_names: list[str],
        evidence: list[dict] | None = None,
    ) -> str:
        template = self._template_explanation(profile, result, source_names, evidence or [])
        if not self._llm.available:
            return template
        try:
            context = json.dumps(
                {
                    "profile": profile.model_dump(),
                    "candidates": result["variants"][:5],
                    "sources": source_names,
                    "evidence": evidence or [],
                },
                ensure_ascii=False,
            )
            system = (
                "你是汽车选车助手的解释模块。只能使用下面给定的候选数据，逐条引用其中的价格、配置与来源；"
                "禁止编造价格、销量、优惠、库存或任何数据中没有的内容；缺失信息如实说明；"
                "不使用表格，不超过 200 字。"
            )
            resp = await self._llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": context}],
                temperature=0.3,
            )
            text = (resp["choices"][0]["message"]["content"] or "").strip()
            ok, _ = safety_guard(text)
            if ok and text:
                return text
        except (LLMError, KeyError, TypeError, IndexError, ValueError):
            pass  # LLM 不可用/响应结构异常：回退确定性模板，Agent 仍可用（原则 7）
        return template

    @staticmethod
    def _template_explanation(
        profile: UserProfile,
        result: dict,
        source_names: list[str],
        evidence: list[dict] | None = None,
    ) -> str:
        top = result["variants"]
        if not top:
            budget_desc = ""
            if profile.budget.max is not None:
                budget_desc = f"（预算不超过 {profile.budget.max / 10000:g} 万元）"
            return (
                f"没有找到完全满足条件{budget_desc}的在售 SKU，建议放宽预算、能源或车身类型后重试；"
                "我们不会编造不存在的数据。"
            )
        first = top[0]
        names = "、".join(f"{v['brand_name']} {v['series_name']} {v['display_name']}" for v in top[:3])
        budget_desc = ""
        if profile.budget.max is not None:
            budget_desc = f"，均在 {profile.budget.max / 10000:g} 万元预算内"
        source_desc = f"（数据来源：{'、'.join(source_names)}）" if source_names else ""
        evidence_desc = ""
        if evidence:
            snippets = "；".join(f"{e['text'][:60]}…" for e in evidence[:2])
            evidence_desc = f"官方资料佐证：{snippets}。"
        return (
            f"为你推荐 {names} 等 {len(top)} 款在售 SKU{budget_desc}。"
            f"首选 {first['brand_name']} {first['series_name']}（官方指导价 {first['price_cny'] / 10000:g} 万元，"
            f"匹配项：{'、'.join(first['matched']) or '综合评分领先'}）。"
            f"注意妥协项：{'；'.join(first['tradeoffs'][:2]) or '无明显妥协'}。"
            f"{evidence_desc}{source_desc}"
        )


_agent_engine: AgentEngine | None = None


def get_agent_engine() -> AgentEngine:
    global _agent_engine
    if _agent_engine is None:
        _agent_engine = AgentEngine()
    return _agent_engine
