"""车型与 SKU 的 API Schema。"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class BrandRef(BaseModel):
    id: int
    name: str


class PriceRange(BaseModel):
    currency: str = "CNY"
    type: str = "official_msrp"
    min: float | None = None
    max: float | None = None


class LatestSales(BaseModel):
    month: str | None = None
    sales_count: int | None = None
    sales_type: str | None = None
    source_name: str | None = None


class ModelYearOut(BaseModel):
    id: int
    year_name: str
    launch_status: str


class VehicleDetailOut(BaseModel):
    id: int
    name: str
    aliases: list[str] = []
    brand: BrandRef | None = None
    body_type: str | None = None
    positioning: str | None = None
    energy_types: list[str] = []
    official_page_url: str | None = None
    thumbnail_url: str | None = None
    active_status: str
    price_range: PriceRange
    latest_sales: LatestSales
    model_years: list[ModelYearOut] = []
    data_updated_at: datetime | None = None
    # 门户来源指导价区间原文（SKU 价格未入库时前端展示回退）
    price_range_note: str | None = None


class SpecFactOut(BaseModel):
    category: str
    fact_key: str
    label: str
    value: str
    unit: str | None = None
    cycle: str | None = None
    display: str


class VariantPriceOut(BaseModel):
    price_cny: float
    price_type: str = "official_msrp"
    effective_from: date | None = None
    effective_to: date | None = None


class VariantOut(BaseModel):
    id: int
    series_id: int
    model_year_id: int
    year_name: str | None = None
    display_name: str
    config_version: str
    powertrain: str
    drivetrain: str
    package: str | None = None
    energy_type: str
    body_type: str | None = None
    status: str
    official_price: VariantPriceOut | None = None
    spec_facts: list[SpecFactOut] = Field(default_factory=list)


class VehicleListItemOut(BaseModel):
    """全部车型浏览列表项（无销量榜排名，供 /vehicles 浏览页使用）。"""

    series_id: int
    series_name: str
    brand_id: int
    brand_name: str
    brand_type: str | None = None
    body_type: str | None = None
    energy_types: list[str] = []
    thumbnail_url: str | None = None
    price_range: PriceRange
    price_range_note: str | None = None
    latest_sales: LatestSales
    source_name: str | None = None
    data_updated_at: datetime | None = None


class VehicleListOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[VehicleListItemOut] = Field(default_factory=list)
