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
    charging_tolerance: bool | None = None
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    unknowns: list[str] = Field(default_factory=list)
    # 用户已点名的车系（会话记忆）：开始就确定车型时，推荐只关注这些车系，
    # 直到用户明确表示「看看其他车」才解锁（评审：指定车型优先）
    locked_series_ids: list[int] = Field(default_factory=list)


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
    official_page_url: str | None = None


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
    official_links: list[str] = Field(default_factory=list)
    explanation: str | None = None
