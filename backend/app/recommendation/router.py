"""确定性推荐接口（推荐算法）。

- POST /api/v1/recommendations：按用户画像（§13.1 同构）执行
  「硬约束（SQL 下推）→ 软评分（权重可调）」的确定性推荐，不调用 LLM。

与 Agent 内部 recommendation_tool 同一实现（app/agent/tools.py），
供前端与外部调用方直接获取候选 SKU（含得分/匹配项/来源），
避免把「推荐能力」只封闭在对话链路里。事实全部来自数据库（原则 3/17）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.schemas import RecommendedVariant
from app.agent.tools import recommendation_tool
from app.common.database import get_session
from app.recommendation.schemas import RecommendationIn, RecommendationOut

router = APIRouter(tags=["recommendation"])


@router.post("/recommendations", response_model=RecommendationOut)
def recommend(
    payload: RecommendationIn,
    db: Session = Depends(get_session),
) -> RecommendationOut:
    """确定性推荐（硬约束 + 8 维软评分）；weights 可覆盖默认权重（§17.2）。"""
    result = recommendation_tool(db, payload, limit=payload.limit)
    return RecommendationOut(
        **payload.model_dump(),
        count=result["count"],
        weights_used=result["weights_used"],
        variants=[RecommendedVariant(**v) for v in result["variants"]],
    )
