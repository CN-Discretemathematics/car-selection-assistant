"""意图路由决策层（P0：决策与执行解耦）。

背景（2026-09 盘点计数缺陷复盘）：engine.respond() 的意图分支靠 regex 堆叠
内联在 1800 行主流程里，每个补丁都在制造下一个补丁。本模块把
「0.6 对比差异分析」往后的确定性分支**按原顺序、原条件**提取为纯决策：

    comparison → series_qa → brand_lineup → catalog_count → tool_loop
    → chitchat → general_advice → recommendation

`decide_route` 只回答「走哪条链路」，不执行任何回复生成；`AgentEngine.respond()`
（handle() 只在外层包回答级耗时日志与路由模式读取）拿到 `RouteDecision` 后按
intent 分发到既有私有方法，执行代码本身不动。
分支顺序与判定条件与提取前的内联实现逐条等价（零行为变更，由 370 项既有
测试与 routing 金标集共同钉住）。

已知死代码顺带消解：原 respond() 在 0.6 与 0.71 有两处重复的
`extract_comparison_variant_ids` 块，0.6 命中即 return，0.71 永远不可达；
提取后只保留一处判定（自然结果，不算独立改动）。

可观测：`log_route_decision` 用 `logging.getLogger("app.agent.routing")`
输出单行 JSON（匿名会话 id / utterance / intent / matched_rule / elapsed_ms）；
不记 IP、不记用户身份。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agent.schemas import UserProfile
from app.agent.series_qa import should_answer
from app.catalog.brands import brand_entries
from app.catalog.series_index import normalize_name

# ── 意图路由正则（自 engine.py 原样迁移，定义不变）──────────────────────────
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

# 品牌盘点类提问（用户实测：「奔驰都有哪些车型」「没有燃油的吗」）：
# 必须读库给出完整盘点，而不是靠模型记忆列举几款——2026-09 实测中模型凭记忆答
# 「我这边能确认的奔驰在售车型有 3 款，都是纯电」，而库里实际有 57 款、燃油 37 款。
_BRAND_LINEUP_RE = re.compile(
    r"(有哪些车型|都有哪些|有哪些车|都有什么车|有什么车型|车型有哪些|车型列表|全系|"
    r"都有啥|有哪些系列|哪些型号)"
)
_ENERGY_AVAILABILITY_RE = re.compile(
    r"((有|要|想看|看看)?(没有|有没有|有)?\s*(燃油|汽油|纯电|插混|增程|油混|新能源)(的|款|车)?\s*(吗|么|嘛|呢|没有)?)"
)
# 明确询问能源是否有货的说法（避免把「我要燃油的」当成盘点：那属于约束，走推荐链）
_ENERGY_ASK_RE = re.compile(
    r"(有(没有)?(燃油|汽油|纯电|插混|增程|油混|新能源)|(燃油|汽油|纯电|插混|增程|油混|新能源)(的)?(吗|么|嘛|呢)|没有(燃油|汽油|纯电|插混|增程|油混|新能源))"
)

# 全库盘点计数（2026-09-17 用户实测：「全部车型有多少款车？」被当成选车需求去追问预算）。
# 只认**数量问法**，不认列举问法（「有哪些车型」由品牌盘点/工具循环接管），
# 也不认配置项计数（「有多少个座位」问的是配置，不是盘点车系）。
# 分支覆盖（2026-09-17 评审 B2：只匹配「多少款+名词」会漏掉修饰词夹在中间的同类问句）：
#   ① 多少/几 +（量词）+ 车系·车型·款型·品牌·车         「全部车型有多少款车」
#   ② 多少/几 + 量词 + ≤5 字修饰 + 名词                 「有多少款新能源车」「有多少款纯电车」
#   ③ 多少/几 + 量词 + 能源/车身类别（省略名词）          「有多少款SUV」
#   ④ 名词 +（有|是）+ 几/多少 +（量词）+（句尾）      「车系有多少」「现在在售车系有几个」
#   ⑤ 名词 + ≤6 字 + 几/多少 +（量词）+（句尾）        「现在在售车型一共几款」
#   ⑥ 名词 + 的? + 总数/数量                            「车系总数是多少」
#   ④⑤ 必须：名词后不得紧跟「的」（排除「这个车型的价格是多少」这类**属性**问句）、
#   且「几/多少」后面要到量词或子句结束——否则「多少」修饰的是车系/品牌的某个属性，
#   会把属性问句答成全库盘点数（2026-09-17 评审二轮阻塞项，16 条误命中全由此来）。
_CATALOG_COUNT_RE = re.compile(
    r"((多少|几)\s*(款|个|台|辆|种)?\s*(车系|车型|款型|品牌|车)"
    r"|(多少|几)\s*(款|个|台|辆|种)\s*[\u4e00-\u9fa5A-Za-z0-9]{1,5}?(车系|车型|款型|品牌|车)"
    r"|(多少|几)\s*(款|个|台|辆|种)\s*(新能源|纯电|插混|增程|油混|燃油|汽油|SUV|MPV|轿车)"
    r"|(车系|车型|款型|品牌)(?!的)\s*(有|是)?\s*(几|多少)\s*(款|个|台|辆|种)?\s*(?=$|[。！？?!，,、；;\s])"
    r"|(车系|车型|款型|品牌)(?!的)[^。！？，,]{0,6}?(几|多少)\s*(款|个|台|辆|种)?\s*(?=$|[。！？?!，,、；;\s])"
    r"|(车系|车型|款型|品牌)\s*的?\s*(总数|数量|总数量))",
    re.IGNORECASE,
)
# 排名/对比/解释语境问的不是「有多少」：拿总数回答排名问题等于答非所问（2026-09-17 评审建议 1）
_CATALOG_COUNT_EXCLUDE_RE = re.compile(r"(最多|最少|排行|排名|对比|区别|解释|为什么|怎么算|是什么意思)")

# 对比页「帮我分析差异」会带上具体款型 ID（前端拼接），Agent 据此做确定性差异分析。
# 两种写法都认：「（款型ID：11、12、13）」与「variant_ids=11,12,13」。
_COMPARE_IDS_RE = re.compile(r"(?:款型\s*ID|variant_ids)\s*[:：=]\s*([0-9、,，\s]+)", re.IGNORECASE)

# 「盘点/对比/解释类自由提问」候选问法（工具循环）
_TOOL_ASSIST_RE = re.compile(
    r"(对比|区别|哪个好|选哪个|优缺点|解释|是什么意思|为什么|"
    r"盘点|有哪些|有哪些系列|都有哪些车|靠谱吗|值得买吗|怎么样)"
)
# 「汽车语境」判定：工具循环只接管与车相关的问法（防「量子纠缠 / 华为 vs 苹果」被劫持）
_CAR_CONTEXT_RE = re.compile(
    r"(车|SUV|MPV|轿车|混动|纯电|增程|燃油|新能源|续航|油耗|动力|配置|指导价|款型|车型|品牌)"
)
# 泛消费电子/无关品类 denylist：单字「车/配置」会误命中「车厘子」「手机配置」（第三轮审查 L2）
# 注意：**不要用单字词**（曾写「表」「房」，把「销量表现」误判成非汽车话题；2026-09-15 实测）
_NON_CAR_RE = re.compile(r"(手机|电脑|相机|耳机|平板|笔记本|车厘子|化妆品|房产|二手房|手表|股票|基金)")
# 易混短品牌名（既是品牌也是日常词）：见 mentions_known_brand 的说明
_AMBIGUOUS_BRAND_NAMES = frozenset(
    {"大众", "现代", "银河", "北京", "长安", "理想", "未来", "启辰", "东风", "红旗", "长城"}
)


def is_chatty(message: str) -> bool:
    return bool(_CHATTY_RE.match(message.strip()))


def has_car_intent(message: str) -> bool:
    return bool(_CAR_INTENT_RE.search(message) or _CAR_BUY_RE.search(message))


def asks_general_advice(message: str) -> bool:
    return bool(_GENERAL_ADVICE_RE.search(message))


def asks_brand_lineup(message: str) -> bool:
    """是否在问「某品牌有哪些车型 / 有没有燃油的」这类需要完整盘点的问题。

    实测缺陷（2026-09）：这类问题此前落到通用对话，由模型凭记忆列举——答成
    「奔驰在售就 3 款，都是纯电」，而库里是 57 款、燃油 37 款。
    """
    return bool(_BRAND_LINEUP_RE.search(message)) or bool(_ENERGY_ASK_RE.search(message))


def asks_catalog_count(message: str) -> bool:
    """是否在问「全库有多少款车/多少个车系」这类需要读库报数的盘点问题。

    排名/对比/解释类措辞一律不算（「哪个品牌车型数量最多」问的是排名，不是总数）。
    """
    if _CATALOG_COUNT_EXCLUDE_RE.search(message):
        return False
    return bool(_CATALOG_COUNT_RE.search(message))


def asks_tool_assist(message: str) -> bool:
    """是否属于盘点/对比/解释类自由提问（工具循环的候选问法）。"""
    return bool(_TOOL_ASSIST_RE.search(message))


def extract_comparison_variant_ids(message: str) -> list[int]:
    """从消息里解析对比款型 ID（缺失则返回空列表，调用方据此决定是否走差异分析）。"""
    match = _COMPARE_IDS_RE.search(message)
    if not match:
        return []
    ids: list[int] = []
    for token in re.findall(r"\d+", match.group(1)):
        value = int(token)
        if value not in ids:
            ids.append(value)
    return ids


def mentions_known_brand(db: Session, message: str) -> bool:
    """消息里是否出现库内品牌名/别名（不看是否构成约束，仅用于判定「汽车语境」）。

    实测缺口（2026-09-15 人工复现）：「解释一下比亚迪的销量表现怎么样」因为
    「比亚迪」不构成硬约束（无「只要/必须」语气）而被判为无汽车语境，整句落到通用对话。

    **易混短名排除**（第三轮审查 M1）：`大众/现代/银河/北京/长安` 既是品牌也是日常词
    （「大众化」「现代人」「银河系」「北京堵车」），2 字子串匹配必然误命中；这些名字
    只在句中已有其它汽车信号时才算数。
    """
    normalized = normalize_name(message)
    if not normalized:
        return False
    ambiguous = _AMBIGUOUS_BRAND_NAMES
    other_signal = bool(_CAR_CONTEXT_RE.search(message) or has_car_intent(message))
    for name, _bid, _label in brand_entries(db):
        if name not in normalized:
            continue
        if len(name) <= 2 and name in ambiguous and not other_signal:
            continue
        return True
    return False


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


# ── 路由决策 ────────────────────────────────────────────────────────────────
# intent 字符串枚举：与 engine 既有分支一一对应，取值固定不收敛进 Enum
# （金标集/日志/测试都用字面量比对，避免引入新的抽象层）。
INTENTS = (
    "comparison",
    "series_qa",
    "brand_lineup",
    "catalog_count",
    "tool_loop",
    "chitchat",
    "general_advice",
    "recommendation",
)


@dataclass
class RouteDecision:
    """一次意图路由的结果。

    intent        命中的链路（INTENTS 之一）；
    matched_rule  命中的分支名与关键正则名（可观测/评测用）；
    slots         分发执行所需的载荷（如 comparison 的款型 ID 列表）；
    signals       判定过程中的关键布尔/计数值（排障用，不参与分发）。
    """

    intent: str
    matched_rule: str
    slots: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)


def decide_route(
    message: str,
    hints: dict,
    structured: bool,
    profile: UserProfile,
    resolved: list,
    db: Session,
) -> RouteDecision:
    """把 respond() 的确定性分支判定提取为纯决策（顺序/条件与原实现逐条等价）。

    参数与 engine.respond() 中游变量一一对应：
    - hints       extract_hints(message) 的结果（本轮可解析线索）；
    - structured  本轮是否有可解析输入（bool(hints)，品牌线索也会置 True）；
    - profile     会话累积画像（含本轮 merge 结果）；
    - resolved    本轮解析出的车系 [(VehicleSeries, Brand|None), ...]；
    - db          会话（tool_loop 的汽车语境判定需查库内品牌名）。
    """
    # 0.6) 对比差异分析（对比页「帮我分析差异」会带上款型 ID）→ 确定性分析 + LLM 措辞。
    #      必须排在**车系档案问答之前**：那句话里同时含车系名，早先会被车系问答截走，
    #      结果退化成「参数罗列」（用户反馈的原问题）。
    #      （原 0.71 处的重复判定不可达，此处只保留一处——见模块 docstring。）
    compare_ids = extract_comparison_variant_ids(message)
    if len(compare_ids) >= 2:
        return RouteDecision(
            intent="comparison",
            matched_rule="0.6:extract_comparison_variant_ids>=2",
            slots={"variant_ids": compare_ids},
            signals={"compare_ids": compare_ids},
        )

    # 车系档案问答（原内联：if resolved and should_answer(resolved, message)）
    if resolved and should_answer(resolved, message):
        return RouteDecision(
            intent="series_qa",
            matched_rule="series_qa:resolved+should_answer",
            signals={"resolved_count": len(resolved), "should_answer": True},
        )

    # 0.7) 品牌盘点（「奔驰都有哪些车型」「没有燃油的吗」「奔驰有多少款车」）→ 读库完整盘点。
    #      必须在「无购车意图 → 普通对话」gate 之前：这类问句不一定是购车意图措辞，
    #      但需要的是库内完整事实，不能交给模型记忆。
    asks_lineup = asks_brand_lineup(message)
    asks_count = asks_catalog_count(message)
    if profile.brand_ids and not resolved and (asks_lineup or asks_count):
        return RouteDecision(
            intent="brand_lineup",
            matched_rule="0.7:brand_ids+not_resolved+(asks_brand_lineup|asks_catalog_count)",
            signals={
                "brand_ids": list(profile.brand_ids),
                "asks_brand_lineup": asks_lineup,
                "asks_catalog_count": asks_count,
            },
        )

    # 0.7b) 全库盘点计数（「全部车型有多少款车」）→ 读库如实报数。
    #       必须在推荐链与工具循环之前：这句话既不含「有哪些」也不含约束词，此前直接落到
    #       「先问一下购车预算」的追问里（2026-09-17 用户实测）。
    #       守卫：① 带核心约束（预算/人数/用途）的计数属筛选场景，交回推荐链；
    #       ② 排名/对比/解释语境不算计数（在 asks_catalog_count 内排除）——
    #          这类问题拿总数回答等于答非所问（评审建议 1）；
    #       ③ 车身/能源偏好不排除，改为按画像报该子集计数——否则「SUV 有多少款车」
    #          会被答成全库数（评审建议 2）。
    #       注：**不**把「盘点」类措辞推给工具循环。评审建议加上 `not asks_tool_assist`，
    #       但工具循环只能看到被截断的工具结果，让它数总数有编造风险（例如把 limit=50 的
    #       结果答成「50 款」）；数量问题一律以确定性计数为准，故此处有意不加该条件。
    core_hint_keys = {"budget", "passengers", "usage"} & set(hints)
    if (
        asks_count
        and not resolved
        and not profile.brand_ids
        and not core_hint_keys
        and profile.budget.min is None
        and profile.budget.max is None
        and not profile.usage
        and profile.passengers is None
    ):
        return RouteDecision(
            intent="catalog_count",
            matched_rule="0.7b:asks_catalog_count+no_resolved+no_brand+no_core_hints",
            signals={
                "body_type": list(profile.body_type or []),
                "energy_preference": list(profile.energy_preference or []),
            },
        )

    # 0.75) 盘点/对比/解释类自由提问 → LLM 工具调用循环（步数受限、全程审计）。
    #       两个前置条件（第二轮审查）：
    #       a) 必须有「汽车语境」（命中车系/品牌/锁定车系/购车词/汽车名词），
    #          防「量子纠缠」「华为 vs 苹果」这类通用问题被劫持；
    #       b) 不得带核心约束（预算/人数/用途）——带约束的继续走推荐链；
    #          车身类型（如「有哪些增程SUV」）不算核心约束：那正是要「列一批」的问法，
    #          实测把它算作核心线索会把这类问题错误地交给追问预算的推荐链（2026-09-15）。
    #       判定表达式与原实现逐字符等价（含 or 短路：mentions_known_brand 只在
    #       前面全部为假时才查库，与原赋值语句的求值顺序一致）。
    car_context = bool(
        resolved
        or profile.brand_ids
        or profile.locked_series_ids
        or _CAR_CONTEXT_RE.search(message)
        or has_car_intent(message)
        or mentions_known_brand(db, message)   # 本条消息提到库内品牌（如「解释一下比亚迪的销量」）
    ) and not _NON_CAR_RE.search(message)
    # 画像层同样只看预算/人数/用途：不能直接用 profile_has_core_constraints（它把 body_type
    # 也算核心约束，而 merge_profile 已把本轮消息里的「SUV」写进画像 → 自己把自己拦掉，
    # 2026-09-15 实测「有哪些增程SUV…」因此落回追问预算）。
    profile_core = (
        profile.budget.min is not None
        or profile.budget.max is not None
        or bool(profile.usage)
        or profile.passengers is not None
    )
    if asks_tool_assist(message) and car_context and not core_hint_keys and not profile_core:
        return RouteDecision(
            intent="tool_loop",
            matched_rule="0.75:asks_tool_assist+car_context+no_core_constraints",
            signals={"car_context": True},
        )

    # 1) 无购车意图的普通对话（如「今天天气不错」「帮我算个题」）→ 自然回复
    if not (has_car_intent(message) or structured):
        return RouteDecision(
            intent="chitchat",
            matched_rule="1:not(has_car_intent or structured)",
            signals={"has_car_intent": False, "structured": False, "car_context": car_context},
        )

    # 2) 通用购车咨询但还没有核心画像（「电动车和油车哪个好」）→ 有据可查的回答，
    #    不硬推「没有预算的推荐」
    if asks_general_advice(message) and not profile_has_core_constraints(profile):
        return RouteDecision(
            intent="general_advice",
            matched_rule="2:asks_general_advice+no_core_profile",
            signals={
                "has_car_intent": has_car_intent(message),
                "structured": structured,
                "car_context": car_context,
            },
        )

    # 4) 结构化画像链路：最小化追问 → 硬筛选 → 软评分（兜底分支）
    return RouteDecision(
        intent="recommendation",
        matched_rule="4:recommendation_fallback",
        signals={
            "has_car_intent": has_car_intent(message),
            "structured": structured,
            "car_context": car_context,
        },
    )


def llm_intent_executable(
    intent: str,
    message: str,
    hints: dict,
    structured: bool,
    profile: UserProfile,
    resolved: list,
    db: Session,
) -> bool:
    """LLM 路由改写 intent 的执行前置条件校验（2026-09-17 评审三轮 B1/B2）。

    LLM 只允许在「该 intent 的确定性执行在当前上下文下确实有效」时改写路由。
    与 decide_route 守卫的关系（评审四轮实测矩阵后定稿，**勿按「逐条一致」回改**）：

    - **保留的门**：series_qa 的 resolved+should_answer（用户刚否定/闲聊提及车系时
      不出档案）；brand_lineup 的 brand_ids+not resolved+盘点问法（会话已锁品牌时的
      车系问答不得被抢成品牌盘点）；catalog_count/tool_loop 的核心约束禁用
      （带预算/人数/用途时执行层会无视约束给出错误答案）；tool_loop 的汽车语境 +
      **计数问法不走工具循环**（工具结果被截断，数总数会编造——Phase 1 决策）；
      chitchat 的无购车意图；general_advice 的 profile_has_core_constraints 口径；
    - **有意放宽的门**：catalog_count/tool_loop 不再要求各自的计数/盘点问法正则——
      这正是 LLM 修 regex 漏识别的价值（「一共有几款」已由金标 known_gap 转正为
      用例钉住）。代价：寒暄句被 LLM 判成 catalog_count 时会答真实库内计数
      （答案本身为真，可接受）；
    - comparison/recommendation 恒可执行（前者 slots 已确定性补齐，后者是兜底链）；
    - 未知 intent 一律 False（回退 regex 决策：宁可不改写，也不错答）。
    """
    core_constraints = (
        bool({"budget", "passengers", "usage"} & set(hints))
        or profile.budget.min is not None
        or profile.budget.max is not None
        or bool(profile.usage)
        or profile.passengers is not None
    )
    if intent == "comparison":
        return True
    if intent == "series_qa":
        return bool(resolved) and should_answer(resolved, message)
    if intent == "brand_lineup":
        return bool(profile.brand_ids) and not resolved and (
            asks_brand_lineup(message) or asks_catalog_count(message)
        )
    if intent == "catalog_count":
        return not core_constraints and not resolved and not profile.brand_ids
    if intent == "tool_loop":
        car_context = bool(
            resolved
            or profile.brand_ids
            or profile.locked_series_ids
            or _CAR_CONTEXT_RE.search(message)
            or has_car_intent(message)
            or mentions_known_brand(db, message)
        ) and not _NON_CAR_RE.search(message)
        return car_context and not core_constraints and not asks_catalog_count(message)
    if intent == "chitchat":
        return not has_car_intent(message) and not structured
    if intent == "general_advice":
        return asks_general_advice(message) and not profile_has_core_constraints(profile)
    if intent == "recommendation":
        return True
    return False


# 路由日志的轻量 PII 掩码：手机号（CN）/邮箱/身份证等长数字串 → ***
# （utterance 是用户原话，可能带个人信息；日志一旦真正落盘就不能原文跟着进去）
_PII_PATTERNS = (
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"\d{15,18}"),
)


def _mask_pii(text: str) -> str:
    for pattern in _PII_PATTERNS:
        text = pattern.sub("***", text)
    return text


def log_route_decision(
    session_id: str, message: str, decision: RouteDecision, elapsed_ms: float
) -> None:
    """单行 JSON 结构化路由日志。

    匿名会话 id = sha256(session_id) 前 12 位（不落原始 id、不记 IP、不记用户身份）；
    utterance 先做轻量 PII 掩码（手机号/邮箱/长数字串 → ***，日志一旦真正落盘，
    用户原文里的可识别信息不能跟着进日志——2026-09-17 评审二轮）再截断到 200 字，
    只服务排障与离线评测回放。
    """
    anon = hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()[:12] or "-"
    payload = {
        "sid": anon,
        "utterance": _mask_pii(message or "")[:200],
        "intent": decision.intent,
        "matched_rule": decision.matched_rule,
        "signals": dict(decision.signals or {}),
        "elapsed_ms": round(float(elapsed_ms), 2),
    }
    logging.getLogger("app.agent.routing").info(json.dumps(payload, ensure_ascii=False))
