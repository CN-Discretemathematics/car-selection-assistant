"""首页 API Schema。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PriceRangeOut(BaseModel):
    currency: str = "CNY"
    type: str = "official_msrp"
    min: float | None = None
    max: float | None = None


class SalesSourceOut(BaseModel):
    id: int | None = None
    name: str | None = None


class HomeCardOut(BaseModel):
    rank: int
    series_id: int
    series_name: str
    brand_id: int
    brand_name: str
    thumbnail_url: str | None = None
    body_type: str | None = None
    energy_types: list[str] = []
    month: str
    sales_count: int
    sales_type: str
    price_range: PriceRangeOut
    source: SalesSourceOut
    data_updated_at: datetime | None = None
    # 门户来源指导价区间原文（SKU 价格未入库时前端展示回退）
    price_range_note: str | None = None
