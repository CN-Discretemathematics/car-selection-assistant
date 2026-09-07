"""管理后台 API Schema。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BrandPatch(BaseModel):
    active_status: Literal["active", "inactive"] | None = None
    official_site: str | None = Field(default=None, max_length=500)
    inclusion_reason: str | None = None
    parent_company: str | None = None
    # 操作说明：写入审计记录（评审 M10）
    note: str | None = Field(default=None, max_length=500)


class SeriesPatch(BaseModel):
    active_status: Literal["active", "inactive"] | None = None
    official_page_url: str | None = Field(default=None, max_length=500)
    positioning: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class VariantPatch(BaseModel):
    status: Literal["on_sale", "off_sale"] | None = None
    note: str | None = Field(default=None, max_length=500)


class ConflictResolve(BaseModel):
    resolution: str = Field(min_length=1, max_length=500)


class BrandAdminOut(BaseModel):
    id: int
    name: str
    brand_type: str
    active_status: str
    official_site: str | None = None
    inclusion_reason: str | None = None
    last_verified_at: datetime | None = None


class ConflictOut(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    field: str
    value_a: str | None = None
    value_b: str | None = None
    source_a_id: int | None = None
    source_b_id: int | None = None
    status: str
    resolution: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class AdminStats(BaseModel):
    brands: int
    series: int
    model_years: int
    variants: int
    prices: int
    facts: int
    sales: int
    source_documents: int
    open_conflicts: int
    users: int
