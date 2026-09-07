"""确定性推荐接口 Schema（13.1 画像同构）。"""
from __future__ import annotations

from pydantic import Field

from app.agent.schemas import RecommendedVariant, UserProfile


class RecommendationIn(UserProfile):
    """推荐请求：完整用户画像（与 Agent 会话画像同构）+ 候选数。

    预算单位为**元**（CNY 官方指导价口径，与 recommendation_tool / OfficialPrice.price_cny
    一致）；用户说「15万」由 extract_hints 换算成 150000，前端展示可自行 ÷10000。
    """

    limit: int = Field(default=5, ge=1, le=10)


class RecommendationOut(UserProfile):
    count: int
    weights_used: dict[str, float] = Field(default_factory=dict)
    variants: list[RecommendedVariant] = Field(default_factory=list)
