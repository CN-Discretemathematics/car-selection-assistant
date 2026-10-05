"""车型与 SKU 的 API Schema。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

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


class ExternalLinkOut(BaseModel):
    """详情页对外的唯一跳转入口：官方车型页优先，缺失时回退到数据来源页。

    优先级判定放在后端（而不是前端三元表达式），这样「官方优先」这条产品口径
    由 pytest 守着——前端只按 kind 决定标签与样式，不再自己判断谁优先。
    """

    kind: Literal["official", "source"]
    url: str
    source_name: str | None = None


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
    # 唯一跳转入口（官方优先 / 回退数据来源）；两者都没有时为 None
    external_link: ExternalLinkOut | None = None
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
    #: 车系名里是否**已经**带上了品牌标识（如「小米SU7」之于「小米汽车」）。
    #: 前端据此决定要不要在车系名前再拼一次品牌——判据由后端给，
    #: 前端 3 处（BrowseCard / CarCard / SearchBar）曾各自重推一遍
    #: `!series_name.startsWith(brand_name)`，那条规则只挡**完全**前缀，
    #: 于是 33 个车系在卡片上显示成「小米汽车小米SU7」「启源长安启源A06」。
    #: 同一规则的第二份 TS 实现必然与后端 `_brand_leads_series` 漂移
    #: （本会话已因「两套独立单位识别器」吃过一次亏），故由后端下发结论。
    show_brand_prefix: bool = True
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
