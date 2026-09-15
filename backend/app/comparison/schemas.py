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
    # 刻意不返回官方车型页 / 来源页链接：对比场景不展示外部跳转入口
    # （口径见 skills/sku-comparison.md；入口只在详情页，官方优先、缺失时回退数据来源）
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
