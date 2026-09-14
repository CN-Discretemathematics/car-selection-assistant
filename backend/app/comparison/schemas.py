"""对比 API Schema。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.common.enums import COMPARISON_MAX_VARIANTS


class ComparisonCreate(BaseModel):
    variant_ids: list[int] = Field(min_length=1, max_length=COMPARISON_MAX_VARIANTS)


class ComparisonSummaryOut(BaseModel):
    id: int
    variant_ids: list[int]
    created_at: datetime


class ComparisonFactOut(BaseModel):
    category: str
    fact_key: str
    label: str
    value: str
    unit: str | None = None
    cycle: str | None = None
    display: str


class CompareVariantOut(BaseModel):
    variant_id: int
    series_id: int
    series_name: str
    brand_name: str
    display_name: str
    energy_type: str
    body_type: str | None = None
    price_cny: float | None = None
    official_page_url: str | None = None
    facts: list[ComparisonFactOut] = Field(default_factory=list)


class CommonParamOut(BaseModel):
    category: str
    fact_key: str
    display: str


class ComparisonDetailOut(BaseModel):
    id: int
    variant_ids: list[int]
    created_at: datetime
    variants: list[CompareVariantOut] = Field(default_factory=list)
    common_params: list[CommonParamOut] = Field(default_factory=list)


# ── 差异分析（2026-09-15：把「参数罗列」变成「决策相关的差异与取舍」）──────────
class AnalysisValueOut(BaseModel):
    variant_id: int
    display: str
    raw: float | None = None
    leader: bool = False


class AnalysisDimensionOut(BaseModel):
    key: str
    label: str
    why: str = ""
    values: list[AnalysisValueOut] = Field(default_factory=list)
    significant: bool = False
    gap: str | None = None
    note: str | None = None


class AnalysisVariantOut(BaseModel):
    variant_id: int
    label: str
    price: float | None = None
    energy_type: str | None = None
    is_new_energy: bool | None = None
    leaders: list[str] = Field(default_factory=list)
    trails: list[str] = Field(default_factory=list)


class AnalysisGapOut(BaseModel):
    dimension: str
    missing: list[str] = Field(default_factory=list)
    note: str = ""


class ComparisonAnalysisOut(BaseModel):
    """差异分析结果：全部字段都是库内事实的确定性推导（无推测、无编造）。"""

    variants: list[AnalysisVariantOut] = Field(default_factory=list)
    price: AnalysisDimensionOut | None = None
    dimensions: list[AnalysisDimensionOut] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)
    gaps: list[AnalysisGapOut] = Field(default_factory=list)
