"""ORM 模型。

层级：brand → vehicle_series → vehicle_model_year → vehicle_variant(SKU)
事实：spec_facts / official_price / monthly_sales / source_documents
质量：data_quality_conflicts（来源冲突记录）
对比：comparisons / comparison_items（登录用户保存；游客用 URL 无状态分享）

约束：
- 只使用可移植列类型，开发 SQLite 与生产 PostgreSQL 共用一套迁移；
- 枚举以字符串存储，取值见 app/common/enums.py；
- 缺失字段一律为 NULL，禁止用 0 或占位字符代替。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Brand(Base):
    """品牌注册表。"""

    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    parent_company: Mapped[str | None] = mapped_column(String(120), nullable=True)
    brand_type: Mapped[str] = mapped_column(String(32), index=True)
    consumer_sales: Mapped[bool] = mapped_column(Boolean, default=True)
    official_site: Mapped[str | None] = mapped_column(String(500), nullable=True)
    active_status: Mapped[str] = mapped_column(String(16), default="active")
    inclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)

    series: Mapped[list["VehicleSeries"]] = relationship(back_populates="brand")


class VehicleSeries(Base):
    """车型系列（首页销量排序与详情页的主体）。"""

    __tablename__ = "vehicle_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    body_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    positioning: Mapped[str | None] = mapped_column(String(120), nullable=True)
    energy_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    official_page_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 门户来源指导价区间原文（如「6.48-9.48万元」），SKU 价格未入库时的展示回退
    price_range_note: Mapped[str | None] = mapped_column(String(80), nullable=True)
    active_status: Mapped[str] = mapped_column(String(16), default="active")
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)

    brand: Mapped["Brand"] = relationship(back_populates="series")
    model_years: Mapped[list["VehicleModelYear"]] = relationship(back_populates="series")
    variants: Mapped[list["VehicleVariant"]] = relationship(back_populates="series")
    sales: Mapped[list["MonthlySales"]] = relationship(back_populates="series")


class VehicleModelYear(Base):
    """年款（系列与 SKU 之间）。"""

    __tablename__ = "vehicle_model_years"
    __table_args__ = (
        UniqueConstraint("series_id", "year_name", name="uq_model_year_series_year"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("vehicle_series.id"), index=True)
    year_name: Mapped[str] = mapped_column(String(40))
    launch_status: Mapped[str] = mapped_column(String(24), default="on_sale")
    launched_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)

    series: Mapped["VehicleSeries"] = relationship(back_populates="model_years")
    variants: Mapped[list["VehicleVariant"]] = relationship(back_populates="model_year")


class VehicleVariant(Base):
    """具体 SKU。

    唯一键不含「地区」（官方指导价全国统一），地区仅作为销售区域属性（sales_regions）。
    
    """

    __tablename__ = "vehicle_variants"
    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "model_year_id",
            "config_version",
            "powertrain",
            "drivetrain",
            "effective_from",
            name="uq_variant_business_key",
        ),
        Index("ix_variants_series_status", "series_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("vehicle_series.id"), index=True)
    model_year_id: Mapped[int] = mapped_column(ForeignKey("vehicle_model_years.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(240))
    config_version: Mapped[str] = mapped_column(String(120))
    powertrain: Mapped[str] = mapped_column(String(80))
    drivetrain: Mapped[str] = mapped_column(String(40))
    package: Mapped[str | None] = mapped_column(String(120), nullable=True)
    energy_type: Mapped[str] = mapped_column(String(16), index=True)
    body_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="on_sale")
    sales_regions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)

    series: Mapped["VehicleSeries"] = relationship(back_populates="variants")
    model_year: Mapped["VehicleModelYear"] = relationship(back_populates="variants")
    facts: Mapped[list["SpecFact"]] = relationship(back_populates="variant", cascade="all, delete-orphan")
    prices: Mapped[list["OfficialPrice"]] = relationship(back_populates="variant", cascade="all, delete-orphan")


class SpecFact(Base):
    """结构化配置事实（参数归一化与软评分的字段来源）。"""

    __tablename__ = "spec_facts"
    __table_args__ = (Index("ix_spec_facts_variant_category", "variant_id", "category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("vehicle_variants.id"), index=True)
    category: Mapped[str] = mapped_column(String(40))
    fact_key: Mapped[str] = mapped_column(String(80))
    fact_value: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(24), nullable=True)
    cycle: Mapped[str | None] = mapped_column(String(16), nullable=True)  # CLTC/NEDC/WLTC
    page_or_section: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    variant: Mapped["VehicleVariant"] = relationship(back_populates="facts")


class OfficialPrice(Base):
    """官方指导价（只允许 official_msrp）。

    禁止写入经销商报价、优惠价、成交价。
    当前生效价 = effective_to IS NULL 的最新一条。
    """

    __tablename__ = "official_prices"
    __table_args__ = (Index("ix_prices_variant_current", "variant_id", "effective_to"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("vehicle_variants.id"), index=True)
    price_cny: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    price_type: Mapped[str] = mapped_column(String(32), default="official_msrp")
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    variant: Mapped["VehicleVariant"] = relationship(back_populates="prices")


class MonthlySales(Base):
    """月销量（口径必须统一并标注）。"""

    __tablename__ = "monthly_sales"
    __table_args__ = (
        UniqueConstraint("series_id", "month", "sales_type", name="uq_sales_series_month_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("vehicle_series.id"), index=True)
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    sales_type: Mapped[str] = mapped_column(String(16), default="retail")
    sales_count: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    series: Mapped["VehicleSeries"] = relationship(back_populates="sales")
    source: Mapped["Source | None"] = relationship()


class Source(Base):
    """数据来源注册表。"""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    source_type: Mapped[str] = mapped_column(String(32))
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    verified_status: Mapped[str] = mapped_column(String(24), default="unverified")
    credibility: Mapped[str] = mapped_column(String(16), default="medium")
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceDocument(Base):
    """原始文件/网页快照的元数据。"""

    __tablename__ = "source_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"), nullable=True, index=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("vehicle_series.id"), nullable=True, index=True)
    model_year_id: Mapped[int | None] = mapped_column(ForeignKey("vehicle_model_years.id"), nullable=True)
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("vehicle_variants.id"), nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32))
    page_or_section: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # 提取后的正文（本地开发存库；生产可仅存对象存储键）
    raw_object_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 对象存储键
    crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verification_status: Mapped[str] = mapped_column(String(24), default="unverified")
    credibility: Mapped[str] = mapped_column(String(16), default="medium")


class DataQualityConflict(Base):
    """同字段来源冲突记录（冲突不静默丢弃）。"""

    __tablename__ = "data_quality_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    field: Mapped[str] = mapped_column(String(80))
    value_a: Mapped[str | None] = mapped_column(String(500), nullable=True)
    value_b: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_a_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    source_b_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Comparison(Base):
    """保存的 SKU 对比（登录用户；游客走 URL 无状态分享）。

    content_hash = 排序后 variant_ids 的摘要：同一组 SKU 的重复创建（分享链接
    反复打开）复用同一行，防止表无限膨胀（评审 P1）。
    """

    __tablename__ = "comparisons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    items: Mapped[list["ComparisonItem"]] = relationship(
        back_populates="comparison", cascade="all, delete-orphan", order_by="ComparisonItem.position"
    )


class User(Base):
    """最小账户：只存账号标识与登录凭据，不收集额外隐私信息。

    注册仅用于收藏；邮箱验证码 + OTP 登录（无密码）；手机号字段预留（短信通道待接入）。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, index=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | disabled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Favorite(Base):
    """收藏（仅针对车型系列或 SKU）。"""

    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "vehicle_id", "kind", name="uq_favorite_user_vehicle_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="series")  # series | variant
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExternalSeriesRef(Base):
    """外部数据源的车系标识映射（如汽车之家 seriesid → 本站 series_id）。

    保证同一外部车系重复导入时归并到同一行，而不是按名称模糊匹配。
    """

    __tablename__ = "external_series_refs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_external_ref_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("vehicle_series.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ComparisonItem(Base):
    __tablename__ = "comparison_items"
    __table_args__ = (
        UniqueConstraint("comparison_id", "variant_id", name="uq_comparison_item_variant"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comparison_id: Mapped[int] = mapped_column(ForeignKey("comparisons.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("vehicle_variants.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    comparison: Mapped["Comparison"] = relationship(back_populates="items")
    variant: Mapped["VehicleVariant"] = relationship()
