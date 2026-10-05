"""Agent API Schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Budget(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str = "CNY"
    type: str = "official_msrp"


class UserProfile(BaseModel):
    """结构化用户画像（13.1）。事实只来自用户对话，不落账户存储。"""

    budget: Budget = Field(default_factory=Budget)
    region: str | None = None
    usage: list[str] = Field(default_factory=list)
    passengers: int | None = None
    body_type: list[str] = Field(default_factory=list)
    energy_preference: list[str] = Field(default_factory=list)
    # 品牌硬约束（用户说「只要奔驰」这类要求）：ids 硬下推 SQL；labels 仅用于回复/前端展示；
    # exclude_ids 承载「不要日系」这类否定要求。取值来自 brands 表（名称 + 别名解析）
    brand_ids: list[int] = Field(default_factory=list)
    brand_labels: list[str] = Field(default_factory=list)
    brand_exclude_ids: list[int] = Field(default_factory=list)
    charging_tolerance: bool | None = None
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    # L4 软缺口追问：已经问过用户「更看重哪一点」的维度键（space/energy/power…）。
    # 非空即**别再问**——真人销售不会把同一个问题问两遍。
    # 存的是**维度键**而不是文案：文案会改，键不会。
    probed_dims: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    # 用户已点名的车系（会话记忆）：开始就确定车型时，推荐只关注这些车系，
    # 直到用户明确表示「看看其他车」才解锁（评审：指定车型优先）
    locked_series_ids: list[int] = Field(default_factory=list)
    # 上一轮**本系统自己推荐过**的款型 id（2026-10-05）。
    #
    # 为什么必须记：用户看到卡片后追问「动力」，此时他**从未点名任何车系**，
    # `locked_series_ids` 是空的 → 检索没有 series 过滤 → 召不回那几台车的参数 →
    # 模型只能说「我手头没有这几款车的动力参数」。而参数明明在库里
    # （捷途旅行者C-DM 最大功率 280kW / 扭矩 610N·m）。
    # 这是诚实性原则的**反向失效**：没查到 ≠ 没有，「说未披露」必须建立在查过的
    # 基础上。记下款型 id，下一轮就能把它们的库内事实直接摆到模型面前。
    last_recommended_variant_ids: list[int] = Field(default_factory=list)


class SessionOut(BaseModel):
    session_id: str


class AgentMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class Clarification(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    source_id: int | None = None
    source_name: str | None = None
    label: str


class RecommendedVariant(BaseModel):
    variant_id: int
    series_id: int
    series_name: str
    brand_name: str
    display_name: str
    energy_type: str
    price_cny: float | None = None
    score: float
    matched: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    # 座位这条硬约束**是否真的校验过**（2026-10-05）。False = 用户点名了乘坐人数，
    # 但库里查不到这台车的座位数，因此既没满足也没筛掉。前端据此打「座位未核实」，
    # 让用户知道自己看到的不是一条已验证的座位结论。默认 True：不点名人数时
    # 本就无座位约束，不该显示任何未核实提示。
    seat_verified: bool = True
    # 不再有 official_page_url：官方车型页链接无法从现有来源获得（docs/deployment.md §8），
    # Agent 侧不提供任何官方/来源跳转（口径：以汽车之家与已入库数据为准）


class AgentMessageOut(BaseModel):
    """13.2 Agent 输出 Schema（含推荐明细与来源引用）。"""

    session_id: str
    need_clarification: bool = False
    clarification: Clarification | None = None
    filters: dict = Field(default_factory=dict)
    recommended_series_ids: list[int] = Field(default_factory=list)
    recommended_variants: list[RecommendedVariant] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    explanation: str | None = None
    # L4 软缺口追问：**随推荐一起给出**的补充问题（不阻塞本轮结果）。
    # 刻意不复用 need_clarification/clarification——那对字段在前端是「二选一」：
    # 设了就不渲染推荐卡片（AgentChat.tsx:430）。软偏好缺失时阻塞推荐属于拖沓，
    # 真人销售是「先给你看车，再问一句你更看重什么」。
    followup: Clarification | None = None
