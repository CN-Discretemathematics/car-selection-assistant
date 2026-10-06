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


def _ranking_intent_skips_consultation(
    hints: dict, resolved: list, profile: UserProfile
) -> bool:
    """用户表达了**可执行**的排序偏好，且没点名具体车系 → 咨询分支是错的去向。

    ## 为什么用「hints 里有没有 weights」而不是「句子里有没有强调词」（2026-10-04）

    第一、二版都用一份**自建**的强调词表做否决，出了两个问题：

    - 词表与 `engine._EMPHASIS_RE` 是两份，靠注释约束同步 → 会漂移；
    - 「我最看重**安全**」这类**算不出权重**的说法也会命中否决，于是被推去推荐链，
      而 8 维里根本没有安全——用户首要诉求被**静默丢弃**。

    `extract_hints` 已经算好了 `hints["weights"]`（= 强调词 ∧ 命中 `_WEIGHT_DIM_KEYWORDS`），
    即「**这一轮真的产生了可执行的偏好**」。直接用它：

    - 零第二份词表，也不需要词表包含关系断言；
    - 算不出权重的强调（安全）自动不否决 → 回到正常咨询，诉求不被吞。

    ## 为什么点名车系时不否决

    已锁定/已点名的两款车，用户问「对比一下」时**对比才是对的**。第一版没看这个，
    把「已锁定 2 车系 + 我最看重动力，对比一下这两款」从差异分析改成了全库重排，
    等于丢掉了用户明确点名的对象（第二版 subagent 审查评为高危）。
    """
    if not hints.get("weights"):
        return False
    return not (resolved or profile.locked_series_ids)
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

# 「对比」动词（深度对比诉求）：「对比一下X和Y」无款型 ID 时归工具循环，不进车系问答
# （2026-09-18 生产 shadow 对拍：双车系解析成功时误入 series_qa，与金标 row36/41 相悖——
# 金标测试种子目录此前缺秦PLUS/海豹06，把这条生产路径掩蔽了）。金标 row27「相比有什么
# 优点」不带「对比」动词，仍是车系问答。
_CONTRAST_ASK_RE = re.compile(r"对比")

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


#: 「什么车卖得好 / 热门榜 / 销量排名」——库里**有**完整月销量（首页就在用同一份），
#: 但此前落到 LLM 后被答成「销量数据不完整」并凭记忆列举。
#: 排除「解释/为什么」语境：那是问原因不是要榜单。
_SALES_RANKING_RE = re.compile(
    r"(热门的?|卖得(好|快)|销量(榜|排名|排行)|最(火|畅销|受欢迎)|"
    r"什么车(最)?(火|畅销|受欢迎)|哪款车(最)?(火|畅销)|卖得最好的|"
    # 2026-10-07：「什么车销量最好」这类**主句式**此前接不住——「销量」只在
    # `销量(榜|排名|排行)` 一个分支里，用户最常问的那句落到了错分支去。
    #
    # **刻意只补带「全库范围」限定词的写法，不补裸 `销量(最好|最高)`**：
    # 「大众和丰田哪个销量好」里两个都是品牌，`resolved` 为空——`not resolved`
    # 那道判断挡得住车系、**挡不住品牌**，裸匹配会把「两品牌对比」变成
    # 「给我一张 Top10」。那是拿新的错换旧的错。
    r"什么车销量(最好|最高|高)|什么车卖得(最好|最多|最快)|哪(款|台|个)车销量(最|排名)|"
    r"哪个品牌销量(最|排名)|卖得最多的是|销量(榜单|榜上|第一)|"
    r"哪(款|台|个)车(卖得|销量)最)"
)
_SALES_RANKING_EXCLUDE_RE = re.compile(r"(为什么|怎么算|解释|是不是真的|准不准)")


def asks_sales_ranking(message: str) -> bool:
    """是否在要一份**销量榜**（读库报数，不该由模型凭记忆列举）。"""
    if _SALES_RANKING_EXCLUDE_RE.search(message):
        return False
    return bool(_SALES_RANKING_RE.search(message))


#: 「几款 / 多少款」这类数量词。`_CATALOG_COUNT_RE` 要求数量词后面紧跟
#: 「车系/车型/款型/品牌/车」这类锚点，于是「奔驰**现在有几款在售**」接不住——
#: 数量词与品牌名之间夹了「现在…在售」，品牌没被锁成约束、`brand_lineup` 进不去。
_BRAND_COUNT_RE = re.compile(r"(多少|几)\s*(款|个|台|辆|种)")


def asks_brand_count(db: Session, message: str) -> bool:
    """「某品牌 + 数量词」盘点（用户 2026-10-05 拍板接进确定性盘点）。

    生产实测（2026-10-05）：同一个品牌，「奔驰有几款车」能答「在售 56 款」，
    「奔驰**现在有几款在售**」却说没有——差别只在这句话的数量词接不住
    `_CATALOG_COUNT_RE`，品牌因此没被锁成硬约束，最后由 LLM 自己决定要不要调工具，
    而它有时调有时不调。走确定性盘点后两类都读库。
    """
    if not _BRAND_COUNT_RE.search(message):
        return False
    if _CATALOG_COUNT_EXCLUDE_RE.search(message):
        return False
    return mentions_known_brand(db, message)


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


def _car_context(
    message: str, profile: UserProfile, resolved: list, db: Session
) -> bool:
    """是否处在汽车语境（工具循环只接管与车相关的问法）。

    单独抽出来是因为 0.65 的对比守卫也要用它——原先守卫与 0.75 各写一份判定，
    两份条件一旦漂移就没有任何东西能发现（见 `_tool_loop_eligible`）。
    """
    return bool(
        resolved
        or profile.brand_ids
        or profile.locked_series_ids
        or _CAR_CONTEXT_RE.search(message)
        or has_car_intent(message)
        or mentions_known_brand(db, message)   # 本条消息提到库内品牌（如「解释一下比亚迪的销量」）
    ) and not _NON_CAR_RE.search(message)


def _tool_loop_eligible(
    message: str,
    hints: dict,
    profile: UserProfile,
    resolved: list,
    db: Session,
    *,
    car_context: bool | None = None,
) -> bool:
    """规则 0.75（工具循环咨询）的**全部**前置条件，唯一实现。

    为什么必须和 0.65 的对比守卫共用一份（2026-10-05 实测缺陷）：
    守卫 `not (_CONTRAST_ASK_RE.search(m) and len(resolved) >= 2)` 的语义是
    「对比问题让给工具循环」——它**假定** 0.75 一定接得住。但 0.75 还要求
    `not profile_core`（画像无预算/人数/用途），守卫没算这一条，于是：

        第1轮「预算20万，汉怎么样」 → 锁定汉
        第2轮「对比一下汉L」        → 并入后 resolved=2，守卫让出车系问答
                                    → 0.75 被 profile_core 拦下
                                    → 一路掉到 4:recommendation_fallback
                                    → 答「预算大概多少？」

    **用户第 1 轮就把预算说过了**，第 2 轮反而被再问一次。实测（真库真 decide_route）：
    该缺口只影响含「对比」二字的消息；「哪个好 / 比呢 / 比一下」等说法不受影响，
    因为 `_CONTRAST_ASK_RE` 只认字面「对比」。

    修法不是给守卫补条件（那只是把同一个 bug 抄第三遍），而是让守卫问一个和
    0.75 **完全同源**的问题：「0.75 真的会接住吗」。接不住就不让出，留在车系问答
    ——它本来就支持多车系对比（`build_series_qa_answer` 的 `" vs "` 拼接）。
    条件收敛到一处后，两边不可能再漂移。

    `car_context` 可由调用方传入已算好的值，避免同一轮里重复查库
    （`mentions_known_brand` 是一次 DB 查询）。
    """
    if car_context is None:
        car_context = _car_context(message, profile, resolved, db)
    core_hint_keys = {"budget", "passengers", "usage"} & set(hints)
    # 画像层只看预算/人数/用途：不能用 profile_has_core_constraints（它把 body_type
    # 也算核心约束，而 merge_profile 已把本轮消息里的「SUV」写进画像 → 自己把自己拦掉，
    # 2026-09-15 实测「有哪些增程SUV…」因此落回追问预算）。
    profile_core = (
        profile.budget.min is not None
        or profile.budget.max is not None
        or bool(profile.usage)
        or profile.passengers is not None
    )
    return (
        asks_tool_assist(message)
        and car_context
        and not core_hint_keys
        and not profile_core
        # 2026-10-04：用户表达了**可执行**的排序偏好时，本分支是错的去向。
        # 「有哪些车推荐」问的是**一批车**，但既然说了看重什么，就该走推荐链按其加权，
        # 而不是丢进工具循环给一段聊天回答。与规则 2 的 general_advice 否决同源。
        and not _ranking_intent_skips_consultation(hints, resolved, profile)
    )


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
        # 0.65) 对比措辞守卫：「对比」动词 + ≥2 个解析车系 → 落 0.75 工具循环，不进车系问答。
        #      **仅当 0.75 确实接得住时才让出**（2026-10-05 实测缺陷，见 `_tool_loop_eligible`
        #      docstring）：原先无条件让出，而 0.75 还要求 `not profile_core`，
        #      于是「第1轮说过预算 → 第2轮对比一下汉L」被反问「预算大概多少？」。
        diverts_to_tool_loop = (
            bool(_CONTRAST_ASK_RE.search(message))
            and len(resolved) >= 2
            and _tool_loop_eligible(message, hints, profile, resolved, db)
        )
        if not diverts_to_tool_loop:
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
    # 2026-10-05（用户拍板）：「奔驰现在有几款在售」的数量词接不住 `asks_catalog_count`
    # （词与品牌名之间夹了「现在…在售」，缺「车系/车型/款型/品牌/车」锚点），
    # 品牌因此没被锁成硬约束 → 最终由 LLM 自己决定要不要调工具，而它有时调有时不调。
    asks_bcount = asks_brand_count(db, message)
    if profile.brand_ids and not resolved and (asks_lineup or asks_count or asks_bcount):
        return RouteDecision(
            intent="brand_lineup",
            matched_rule="0.7:brand_ids+not_resolved+(asks_brand_lineup|asks_catalog_count|asks_brand_count)",
            signals={
                "brand_ids": list(profile.brand_ids),
                "asks_brand_lineup": asks_lineup,
                "asks_catalog_count": asks_count,
                "asks_brand_count": asks_bcount,
            },
        )

    # 0.7a2) 销量/热门榜（「什么车卖得好」「本月销量榜」）→ 读库如实给榜单。
    #        生产实测（2026-10-05）：该问题落到 LLM 后被答成
    #        「销量数据不完整，没法给你准确的热门榜」，并凭记忆列举了速腾/凯美瑞/
    #        卡罗拉——**三款都不是销冠**（销冠是星愿 39,651 辆），而首页正下方
        #        就是同一份榜单。**该能答的说没有、不能答的凭记忆答**，两头都错。
    #        守卫：不与「已点名车系」共存（「汉卖得好吗」是单车系问题，走档案/检索）。
    if asks_sales_ranking(message) and not resolved:
        return RouteDecision(
            intent="sales_ranking",
            matched_rule="0.7a2:asks_sales_ranking+no_resolved",
            signals={"body_type": list(profile.body_type or [])},
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
    #       前置条件（第二轮审查）：必须有「汽车语境」（防「量子纠缠」「华为 vs 苹果」
    #       被劫持）；不得带核心约束（预算/人数/用途）——带约束的继续走推荐链；
    #       车身类型（如「有哪些增程SUV」）不算核心约束：那正是要「列一批」的问法，
    #       实测把它算作核心线索会把这类问题错误地交给追问预算的推荐链（2026-09-15）。
    #       条件本体已抽到 `_tool_loop_eligible`（与 0.65 对比守卫共用同一份判定，
    #       2026-10-05 起），这里只负责把它和 car_context 算给下游 signals 用。
    car_context = _car_context(message, profile, resolved, db)
    if _tool_loop_eligible(
        message, hints, profile, resolved, db, car_context=car_context
    ):
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
    #
    # 2026-10-04 修：用户**表达了排序意图**时不得走 general_advice / tool_loop。
    # 实测缺陷（生产口径，复刻 engine.respond 的顺序：先 merge_profile 再 decide_route）：
    #   「我最看重动力，有哪些车推荐」 → tool_loop   → 0 张卡片
    #   「我最看重动力，怎么选」       → general_advice → 0 张卡片
    # 两者都表达「按这个来排」，却被两个「咨询类」分支分别抢走。
    #
    # 根因：`比较/对比/有哪些/怎么样` 这些词是**问法**，没有区分「要对比」与
    # 「要你替我挑」。而 `profile_core`（0.75 分支）刻意不含 body_type，
    # `profile_has_core_constraints`（规则 2）虽含 body_type，但「我最看重动力」
    # 这类句子根本不提车型——两边都判不出他在表达偏好。
    #
    # 修法：`_ranking_intent_skips_consultation`（本文件内定义，**零第二份词表**）
    # 一处否决**两个**咨询分支；判据是「本轮真算出了可执行权重」而不是「句子里
    # 出现了强调词」，因此「我最看重安全」这种算不出权重的说法**不会**被误推去推荐。
    if (
        asks_general_advice(message)
        and not profile_has_core_constraints(profile)
        and not _ranking_intent_skips_consultation(hints, resolved, profile)
    ):
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
            asks_brand_lineup(message)
            or asks_catalog_count(message)
            or asks_brand_count(db, message)
        )
    if intent == "catalog_count":
        return not core_constraints and not resolved and not profile.brand_ids
    if intent == "sales_ranking":
        return not resolved and asks_sales_ranking(message)
    if intent == "tool_loop":
        # 同 decide_route 的 0.75 分支：带排序意图时 tool_loop 是错的执行分支。
        # （原先把否决写在这个 return **之后**，是死代码——LLM 改写照样能从这扇门进来。）
        if _ranking_intent_skips_consultation(hints, resolved, profile):
            return False
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
        # 与 decide_route 规则 2 保持**同一份**判据（含排序意图否决），
        # 否则一致性校验会和真实决策打架，出现「日志说通过、实际走了别的分支」。
        return (
            asks_general_advice(message)
            and not profile_has_core_constraints(profile)
            and not _ranking_intent_skips_consultation(hints, resolved, profile)
        )
    if intent == "recommendation":
        return True
    return False


# 路由日志的轻量 PII 掩码：手机号（CN）/邮箱/身份证等长数字串 → ***
# （utterance 是用户原话，可能带个人信息；日志一旦真正落盘就不能原文跟着进去）
_PII_PATTERNS = (
    # ⚠️ 三条都要**数字边界**（2026-10-06 独立审查 P1-5 实测）：无边界的 `1[3-9]\d{9}`
    # 会在一段更长的数字串**中间**匹配到 11 位并把它替换掉：
    #     110101199001011234（18 位身份证） -> 110101***4
    # 边界让它只在「恰好 11 位、且左右都不是数字」时才命中。
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"(?<!\d)\d{15,18}(?!\d)"),
    # 2026-10-06（独立审查实测补齐）：上面那条只认「≥15 位**连续**数字」，于是
    # **身份证分段写**（前 6 位地区码 + 生日 8 位，分两三次打进对话）
    # 完全漏掉——完整 18 位反而会被命中，拆开写就漏。汽车问答里「我身份证号是
    # 110101 和 19900101 这样的」并不罕见，故补分段模式。
    #
    # ⚠️ 但**不能**靠两条前瞻式正则按顺序逐条 sub：第一段被换成 `***` 之后，
    # 第二段就找不到它的参照物了（第一版就是这样：地区码被掩、生日段漏）。
    # 故分段身份证改由 `_mask_split_id` **一次扫完整对**再替换。
)

#: 身份证**分段**：6 位地区码 + 8 位生日，顺序不定，中间可隔 0~8 个非数字字符。
_SPLIT_ID_RE = re.compile(
    r"(\d{6})([\s\S]{0,8}?)(\d{8})|(\d{8})([\s\S]{0,8}?)(\d{6})"
)


def _mask_split_id(text: str) -> str:
    """一次扫完整对再替换（见 `_SPLIT_ID_RE` 处的说明）。"""

    def _repl(m: re.Match) -> str:
        return "***" + (m.group(2) or m.group(5) or "") + "***"

    return _SPLIT_ID_RE.sub(_repl, text)


#: 车牌（苏A12345 / 京A·88888）——**必须带上下文词**才脱敏。
#:
#: 2026-10-06（独立审查 P1-4，实测 8/8 真实车名被抹）：第一版直接用
#: `[一-龥][A-Z][·]?[A-Z0-9]{5,6}`，而**车名的字面形状与车牌完全一样**：
#:     长安CS75PLUS   → 长***S        五菱宏光MINIEV → 五菱宏***
#:     赛那SIENNA     → 赛***          大双GW4D20M    → 大***
#: 更糟的是 `_mask_text` 也用在**回复正文**上，于是用户看到的是被抹花的车型名——
#: 为了脱敏反而**破坏了答案本身**。全库实测 **195 条**真实车系/版本/参数文本受损。
#:
#: 故改为**上下文触发**：只在「车牌/牌照/号牌」等词附近才认。
#: 代价是裸写的「苏A12345」会漏——但在一门会把车型名当成 PII 的脱敏器里，
#: **宁可漏也不许毁**。这是明确的取舍，不是疏漏。
#:
#: ⚠️ 2026-10-06 独立审查 P1-1：**分隔必须允许系动词「是/为」**，否则最自然的
#: 中文说法全漏（实测）：
#:     我的车牌苏A12345   -> 我的车牌***            ✅
#:     我的车牌是苏A12345 -> 我的车牌是苏A12345     ❌ 漏（这才是真实说法）
#:     车牌号是苏A12345   -> 车牌号是苏A12345       ❌ 漏
#: 即：加了上下文闸却漏掉系动词，等于「用 102 条真实车名换了车牌明文入库」——
#: **这不是可接受的取舍**，是第一版闸写得太窄。
_PLATE_RE = re.compile(
    r"(车牌号?|牌照|号牌)\s*(?:是|为)?\s*[:：]?\s*(?:是|为)?\s*[一-龥][A-Z][·]?[A-Z0-9]{5,6}"
)


def _mask_plate(text: str) -> str:
    return _PLATE_RE.sub(lambda m: f"{m.group(1)}***", text)


def _mask_pii(text: str) -> str:
    # ⚠️ 顺序要紧（2026-10-06 独立审查 P1-5 实测）：`_mask_split_id` **必须放在最后**。
    # 放在最前时它的 `\d{6}`/`\d{8}` 会先把长数字串切碎，后面的手机号与 ≥15 位规则
    # 就再也匹配不到完整串了：
    #     1234567890123456   ->  ******56    （本该全掩，泄漏尾 2 位）
    #     13800138000        ->  ***000      （本该全掩，泄漏尾 3 位）
    # 完整串交给通用规则，分段的那一对由 _mask_split_id 单独一趟收尾。
    for pattern in _PII_PATTERNS:
        text = pattern.sub("***", text)
    text = _mask_split_id(text)
    return _mask_plate(text)


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
