"""数据导入服务（数据管线：解析 → 标准化 → 归一化 → 校验 → 入库）。

规则：
- 导入前先做枚举与必填校验，任何校验错误都不写库（全量失败）；
- 品牌/车系/年款/SKU 按业务键 upsert（SKU 归一化）；
- 同字段冲突时按来源优先级处理：官方来源优先；低优先级来源的差异
  只记录到 data_quality_conflicts，不覆盖现有数据（§8.2：冲突不静默丢弃）；
- 价格冲突且新来源更优时：关闭旧指导价（effective_to），写入新价，形成历史；
- 本工具导入结构化事实（品牌/车系/SKU/价格/销量）；原始文件与网页快照的
  source_documents 记录由抓取管线单独写入（对象存储 + 正文提取）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.enums import (
    BODY_TYPES,
    BRAND_TYPES,
    CREDIBILITY_LEVELS,
    CYCLES,
    ENERGY_TYPES,
    MODEL_YEAR_STATUSES,
    SALES_TYPES,
    SOURCE_TYPES,
    VARIANT_STATUSES,
    VERIFICATION_STATUSES,
)
from app.common.models import (
    Brand,
    DataQualityConflict,
    ExternalSeriesRef,
    MonthlySales,
    OfficialPrice,
    Source,
    SpecFact,
    VehicleModelYear,
    VehicleSeries,
    VehicleVariant,
)

SOURCE_RANK = {
    "official_site": 5,
    "official_doc": 4,
    "licensed_data": 3,
    "industry_data": 2,
    "other": 1,
    "user_review": 0,
}

# 占位品牌：排名/榜单导入时挂靠的「未知品牌」桶，仅允许 待分类 → 真实品牌 的单向迁移；
# 真实品牌不得被榜单重导入迁回占位品牌（回归：星愿/Model Y 显示「待分类」）
PLACEHOLDER_BRANDS = {"待分类（汽车之家销量榜）"}


@dataclass
class ImportReport:
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def bump(self, entity: str) -> None:
        self.created[entity] = self.created.get(entity, 0) + 1

    def bump_update(self, entity: str) -> None:
        self.updated[entity] = self.updated.get(entity, 0) + 1

    @property
    def ok(self) -> bool:
        return not self.errors


def _norm(value) -> str:
    if isinstance(value, Decimal):
        return f"dec:{value}"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def validate_payload(payload: dict) -> list[str]:
    """结构 + 枚举校验；返回错误列表（为空即通过）。"""
    errors: list[str] = []
    source = payload.get("source") or {}
    if not source.get("name"):
        errors.append("source.name 必填")
    if source.get("source_type") not in SOURCE_TYPES:
        errors.append(f"source.source_type 非法：{source.get('source_type')}")
    if source.get("credibility") not in CREDIBILITY_LEVELS:
        errors.append(f"source.credibility 非法：{source.get('credibility')}")
    if source.get("verified_status") is not None and source["verified_status"] not in VERIFICATION_STATUSES:
        errors.append(f"source.verified_status 非法：{source.get('verified_status')}")

    brands = payload.get("brands") or []
    for i, b in enumerate(brands):
        if not b.get("name"):
            errors.append(f"brands[{i}].name 必填")
        if b.get("brand_type") not in BRAND_TYPES:
            errors.append(f"brands[{i}].brand_type 非法：{b.get('brand_type')}")

    series_list = payload.get("series") or []
    for i, s in enumerate(series_list):
        tag = f"series[{i}]"
        if not s.get("name") or not s.get("brand"):
            errors.append(f"{tag}.name/brand 必填")
        if s.get("body_type") is not None and s["body_type"] not in BODY_TYPES:
            errors.append(f"{tag}.body_type 非法：{s.get('body_type')}")
        for t in s.get("energy_types") or []:
            if t not in ENERGY_TYPES:
                errors.append(f"{tag}.energy_types 非法：{t}")
        for j, y in enumerate(s.get("model_years") or []):
            if not isinstance(y, dict) or not y.get("year_name"):
                errors.append(f"{tag}.model_years[{j}] 必须为对象且 year_name 必填")
                continue
            if y.get("launch_status") is not None and y["launch_status"] not in MODEL_YEAR_STATUSES:
                errors.append(f"{tag}.model_years[{j}].launch_status 非法：{y.get('launch_status')}")
            for k, v in enumerate(y.get("variants") or []):
                if not isinstance(v, dict):
                    errors.append(f"{tag}.model_years[{j}].variants[{k}] 必须为对象")
                    continue
                vtag = f"{tag}.model_years[{j}].variants[{k}]"
                for req in ("config_version", "powertrain", "drivetrain", "energy_type"):
                    if not v.get(req):
                        errors.append(f"{vtag}.{req} 必填")
                if v.get("energy_type") not in ENERGY_TYPES:
                    errors.append(f"{vtag}.energy_type 非法：{v.get('energy_type')}")
                if v.get("status") is not None and v["status"] not in VARIANT_STATUSES:
                    errors.append(f"{vtag}.status 非法：{v.get('status')}")
                price = v.get("price_cny")
                if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float))):
                    errors.append(f"{vtag}.price_cny 必须为数字或省略（无官方指导价时）")
                raw_effective = v.get("effective_from")
                if raw_effective is not None:
                    if isinstance(raw_effective, str):
                        try:
                            date.fromisoformat(raw_effective)
                        except ValueError:
                            errors.append(f"{vtag}.effective_from 非法日期：{raw_effective}")
                    elif not isinstance(raw_effective, date):
                        errors.append(f"{vtag}.effective_from 必须为 YYYY-MM-DD 字符串")
                for f_i, f in enumerate(v.get("facts") or []):
                    for req in ("category", "fact_key", "value"):
                        if not f.get(req):
                            errors.append(f"{vtag}.facts[{f_i}].{req} 必填")
                    if f.get("cycle") and f["cycle"] not in CYCLES:
                        errors.append(f"{vtag}.facts[{f_i}].cycle 非法：{f['cycle']}")

    sales_list = payload.get("sales") or []
    for i, sl in enumerate(sales_list):
        if not isinstance(sl, dict) or not sl.get("month") or sl.get("count") is None:
            errors.append(f"sales[{i}].month/count 必填")
            continue
        if not sl.get("series") and not sl.get("external_id"):
            errors.append(f"sales[{i}] 需要 series（车系名）或 external_id（外部车系标识）")
        if isinstance(sl["count"], bool) or not isinstance(sl["count"], int):
            errors.append(f"sales[{i}].count 必须为整数")
        # sales_type 缺省为 retail（与导入逻辑一致），仅在提供时校验枚举
        if sl.get("sales_type") is not None and sl["sales_type"] not in SALES_TYPES:
            errors.append(f"sales[{i}].sales_type 非法：{sl.get('sales_type')}")
    return errors[:100]


def _resolve(db: Session, report: ImportReport, entity_type: str, entity_id: int, field: str,
             old_value, new_value, old_source: Source | None, new_source: Source) -> bool:
    """字段级冲突处理：返回是否采用新值。不采用时记录冲突。"""
    if _norm(old_value) == _norm(new_value):
        return False  # 无变化
    if old_source is None:
        return True  # 首次填充
    if old_source.id == new_source.id:
        return True  # 同一来源更新自己的数据（如汽车之家先导榜单、后补车系详情）
    rank_old = SOURCE_RANK.get(old_source.source_type, 0)
    rank_new = SOURCE_RANK.get(new_source.source_type, 0)
    if rank_new > rank_old:
        return True
    db.add(
        DataQualityConflict(
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            # 截断到列宽（String(500)）：fact_value 可达 2000 字符，PostgreSQL 会因
            # StringDataRightTruncation 让整个导入 500（SQLite 开发态不报错，评审 M-R4）
            value_a=str(old_value)[:500],
            value_b=str(new_value)[:500],
            source_a_id=old_source.id,
            source_b_id=new_source.id,
            status="open",
        )
    )
    report.conflicts.append(f"{entity_type}#{entity_id}.{field}: 保留 {old_source.name} 的值（优先级更高）")
    return False


def import_catalog(db: Session, payload: dict) -> ImportReport:
    report = ImportReport()
    report.errors = validate_payload(payload)
    if not report.ok:
        return report

    source_cfg = payload["source"]
    source = db.scalar(select(Source).where(Source.name == source_cfg["name"]))
    if source is None:
        source = Source(
            name=source_cfg["name"],
            source_type=source_cfg["source_type"],
            url=source_cfg.get("url"),
            verified_status=source_cfg.get("verified_status", "unverified"),
            credibility=source_cfg.get("credibility", "medium"),
            last_verified_at=datetime.now(timezone.utc),
        )
        db.add(source)
        db.flush()
        report.bump("source")
    else:
        report.bump_update("source")

    for b_cfg in payload.get("brands") or []:
        brand = db.scalar(select(Brand).where(Brand.name == b_cfg["name"]))
        if brand is None:
            brand = Brand(
                name=b_cfg["name"],
                aliases=b_cfg.get("aliases") or [],
                parent_company=b_cfg.get("parent_company"),
                brand_type=b_cfg["brand_type"],
                official_site=b_cfg.get("official_site"),
                inclusion_reason=b_cfg.get("inclusion_reason"),
                active_status="active",
                source_id=source.id,
                last_verified_at=datetime.now(timezone.utc),
            )
            db.add(brand)
            db.flush()
            report.bump("brand")
        else:
            for field in ("brand_type", "official_site", "parent_company", "aliases"):
                new_value = b_cfg.get(field)
                if new_value is None:
                    continue
                old_value = getattr(brand, field)
                old_source = db.get(Source, brand.source_id) if brand.source_id else None
                if _resolve(db, report, "brand", brand.id, field, old_value, new_value, old_source, source):
                    setattr(brand, field, new_value)
                    brand.source_id = source.id
                    brand.last_verified_at = datetime.now(timezone.utc)
                    report.bump_update("brand")

    for s_cfg in payload.get("series") or []:
        brand = db.scalar(select(Brand).where(Brand.name == s_cfg["brand"]))
        if brand is None:
            report.errors.append(f"series.brand 不存在：{s_cfg['brand']}")
            continue
        series = None
        # 外部标识优先：同一外部车系归并到既有行（可跨品牌迁移，如从「待分类」归入真实品牌）
        if s_cfg.get("external_id"):
            ref = db.scalar(
                select(ExternalSeriesRef).where(
                    ExternalSeriesRef.source_id == source.id,
                    ExternalSeriesRef.external_id == str(s_cfg["external_id"]),
                )
            )
            if ref is not None:
                series = db.get(VehicleSeries, ref.series_id)
        if series is None:
            series = db.scalar(
                select(VehicleSeries).where(
                    VehicleSeries.brand_id == brand.id, VehicleSeries.name == s_cfg["name"]
                )
            )
        if series is None:
            series = VehicleSeries(
                brand_id=brand.id,
                name=s_cfg["name"],
                aliases=s_cfg.get("aliases") or [],
                body_type=s_cfg.get("body_type"),
                positioning=s_cfg.get("positioning"),
                energy_types=s_cfg.get("energy_types") or [],
                official_page_url=s_cfg.get("official_page_url"),
                thumbnail_url=s_cfg.get("thumbnail_url"),
                price_range_note=s_cfg.get("price_range_note"),
                active_status="active",
                source_id=source.id,
                last_verified_at=datetime.now(timezone.utc),
            )
            db.add(series)
            db.flush()
            report.bump("series")
        else:
            # 跨品牌归并：外部标识指向的车系若挂在「待分类」品牌下，迁移到真实品牌；
            # 反向（真实 → 占位品牌）禁止，防止榜单类数据重导入把品牌迁回占位桶
            if series.brand_id != brand.id and brand.name not in PLACEHOLDER_BRANDS:
                series.brand_id = brand.id
                series.last_verified_at = datetime.now(timezone.utc)
                report.bump_update("series")
            for field in (
                "body_type",
                "energy_types",
                "positioning",
                "official_page_url",
                "thumbnail_url",
                "price_range_note",
            ):
                new_value = s_cfg.get(field)
                if new_value is None:
                    continue
                old_value = getattr(series, field)
                old_source = db.get(Source, series.source_id) if series.source_id else None
                if _resolve(db, report, "series", series.id, field, old_value, new_value, old_source, source):
                    setattr(series, field, new_value)
                    series.source_id = source.id
                    series.last_verified_at = datetime.now(timezone.utc)
                    report.bump_update("series")

        # 外部来源车系标识（如汽车之家 seriesid）：重复导入归并到同一行
        if s_cfg.get("external_id"):
            external_id = str(s_cfg["external_id"])
            ref = db.scalar(
                select(ExternalSeriesRef).where(
                    ExternalSeriesRef.source_id == source.id,
                    ExternalSeriesRef.external_id == external_id,
                )
            )
            if ref is None:
                db.add(ExternalSeriesRef(source_id=source.id, external_id=external_id, series_id=series.id))
            elif ref.series_id != series.id:
                ref.series_id = series.id

        for y_cfg in s_cfg.get("model_years") or []:
            year = db.scalar(
                select(VehicleModelYear).where(
                    VehicleModelYear.series_id == series.id,
                    VehicleModelYear.year_name == y_cfg["year_name"],
                )
            )
            if year is None:
                year = VehicleModelYear(
                    series_id=series.id,
                    year_name=y_cfg["year_name"],
                    launch_status=y_cfg.get("launch_status", "on_sale"),
                    source_id=source.id,
                )
                db.add(year)
                db.flush()
                report.bump("model_year")
            elif y_cfg.get("launch_status") and year.launch_status != y_cfg["launch_status"]:
                # §6.1：年款上市状态随最新来源更新（同源更新直接生效）
                old_source = db.get(Source, year.source_id) if year.source_id else None
                if _resolve(db, report, "model_year", year.id, "launch_status", year.launch_status,
                            y_cfg["launch_status"], old_source, source):
                    year.launch_status = y_cfg["launch_status"]
                    report.bump_update("model_year")
            for v_cfg in y_cfg.get("variants") or []:
                _import_variant(db, report, series, year, v_cfg, source)

    for sl in payload.get("sales") or []:
        series = None
        if sl.get("external_id"):
            ref = db.scalar(
                select(ExternalSeriesRef).where(
                    ExternalSeriesRef.source_id == source.id,
                    ExternalSeriesRef.external_id == str(sl["external_id"]),
                )
            )
            if ref is not None:
                series = db.get(VehicleSeries, ref.series_id)
        if series is None and sl.get("series"):
            series = db.scalars(
                select(VehicleSeries).join(Brand, VehicleSeries.brand_id == Brand.id).where(
                    VehicleSeries.name == sl["series"]
                )
            ).first()
        if series is None:
            report.errors.append(f"sales 无法定位车系：{sl.get('series') or sl.get('external_id')}")
            continue
        _import_sales(db, report, series, sl, source)

    if report.errors:
        db.rollback()  # 处理期出现错误：整体回滚，避免半成品入库
    else:
        db.commit()
    return report


def _import_variant(db: Session, report: ImportReport, series: VehicleSeries, year: VehicleModelYear,
                    v_cfg: dict, source: Source) -> None:
    raw_effective = v_cfg.get("effective_from") or date.today()
    effective_from = (
        date.fromisoformat(raw_effective) if isinstance(raw_effective, str) else raw_effective
    )
    # SKU 归一化：查找键不含 effective_from（同一配置版本合并更新，避免重复导入产生重复 SKU）；
    # 模型唯一键含 effective_from，用于手工维护历史版本共存（本项目导入路径不使用该能力）。
    variant = db.scalar(
        select(VehicleVariant).where(
            VehicleVariant.series_id == series.id,
            VehicleVariant.model_year_id == year.id,
            VehicleVariant.config_version == v_cfg["config_version"],
            VehicleVariant.powertrain == v_cfg["powertrain"],
            VehicleVariant.drivetrain == v_cfg["drivetrain"],
        )
    )
    if variant is None:
        variant = VehicleVariant(
            series_id=series.id,
            model_year_id=year.id,
            display_name=f"{v_cfg.get('display_name') or series.name + ' ' + year.year_name + ' ' + v_cfg['config_version']}",
            config_version=v_cfg["config_version"],
            powertrain=v_cfg["powertrain"],
            drivetrain=v_cfg["drivetrain"],
            package=v_cfg.get("package"),
            energy_type=v_cfg["energy_type"],
            body_type=v_cfg.get("body_type") or series.body_type,
            status=v_cfg.get("status", "on_sale"),
            effective_from=effective_from,
            source_id=source.id,
        )
        db.add(variant)
        db.flush()
        report.bump("variant")
        if v_cfg.get("price_cny") is not None:
            _set_price(db, report, variant, Decimal(str(v_cfg["price_cny"])), source)
        _set_facts(db, report, variant, v_cfg.get("facts") or [], source)
        return

    for field in ("display_name", "energy_type", "body_type", "package", "status"):
        new_value = v_cfg.get(field)
        if new_value is None:
            continue
        old_value = getattr(variant, field)
        old_source = db.get(Source, variant.source_id) if variant.source_id else None
        if _resolve(db, report, "variant", variant.id, field, old_value, new_value, old_source, source):
            setattr(variant, field, new_value)
            report.bump_update("variant")

    if v_cfg.get("price_cny") is not None:
        _set_price(db, report, variant, Decimal(str(v_cfg["price_cny"])), source)
    _set_facts(db, report, variant, v_cfg.get("facts") or [], source)


def _current_price(db: Session, variant_id: int) -> OfficialPrice | None:
    return db.scalars(
        select(OfficialPrice).where(
            OfficialPrice.variant_id == variant_id,
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
    ).first()


def _set_price(db: Session, report: ImportReport, variant: VehicleVariant, price: Decimal,
               source: Source) -> None:
    current = _current_price(db, variant.id)
    if current is None:
        db.add(
            OfficialPrice(
                variant_id=variant.id,
                price_cny=price,
                price_type="official_msrp",
                source_id=source.id,
                effective_from=date.today(),
                last_verified_at=datetime.now(timezone.utc),
            )
        )
        report.bump("price")
        return
    if current.price_cny == price:
        return
    old_source = db.get(Source, current.source_id) if current.source_id else None
    if _resolve(db, report, "variant", variant.id, "official_price", current.price_cny, price, old_source, source):
        current.effective_to = date.today()  # 关闭旧指导价，形成历史
        db.add(
            OfficialPrice(
                variant_id=variant.id,
                price_cny=price,
                price_type="official_msrp",
                source_id=source.id,
                effective_from=date.today(),
                last_verified_at=datetime.now(timezone.utc),
            )
        )
        report.bump_update("price")


def _set_facts(db: Session, report: ImportReport, variant: VehicleVariant, fact_cfgs: list[dict],
               source: Source) -> None:
    """批量写入/更新 SKU 事实（语义与逐条 _set_fact 一致，仅减少查询次数）。

    每个变体只查询一次既有事实并批量插入，适配大配置表（每车系数千条事实）。
    """
    if not fact_cfgs:
        return
    existing_map = {
        (f.category, f.fact_key): f
        for f in db.scalars(select(SpecFact).where(SpecFact.variant_id == variant.id)).all()
    }
    for f_cfg in fact_cfgs:
        existing = existing_map.get((f_cfg["category"], f_cfg["fact_key"]))
        if existing is None:
            db.add(
                SpecFact(
                    variant_id=variant.id,
                    category=f_cfg["category"],
                    fact_key=f_cfg["fact_key"],
                    fact_value=f_cfg["value"],
                    unit=f_cfg.get("unit"),
                    cycle=f_cfg.get("cycle"),
                    page_or_section=f_cfg.get("page_or_section"),
                    source_id=source.id,
                    last_verified_at=datetime.now(timezone.utc),
                )
            )
            report.bump("fact")
            continue
        if (
            existing.fact_value == f_cfg["value"]
            and existing.unit == f_cfg.get("unit")
            and existing.cycle == f_cfg.get("cycle")
        ):
            continue
        old_source = db.get(Source, existing.source_id) if existing.source_id else None
        new_value = (f_cfg["value"], f_cfg.get("unit"), f_cfg.get("cycle"))
        old_value = (existing.fact_value, existing.unit, existing.cycle)
        if _resolve(db, report, "variant", variant.id, f"{f_cfg['category']}.{f_cfg['fact_key']}", old_value, new_value, old_source, source):
            existing.fact_value = f_cfg["value"]
            existing.unit = f_cfg.get("unit")
            existing.cycle = f_cfg.get("cycle")
            existing.source_id = source.id
            existing.last_verified_at = datetime.now(timezone.utc)
            report.bump_update("fact")


def _import_sales(db: Session, report: ImportReport, series: VehicleSeries, sl: dict, source: Source) -> None:
    existing = db.scalars(
        select(MonthlySales).where(
            MonthlySales.series_id == series.id,
            MonthlySales.month == sl["month"],
            MonthlySales.sales_type == sl.get("sales_type", "retail"),
        )
    ).first()
    if existing is None:
        db.add(
            MonthlySales(
                series_id=series.id,
                month=sl["month"],
                sales_type=sl.get("sales_type", "retail"),
                sales_count=sl["count"],
                source_id=source.id,
                published_at=sl.get("published_at"),
                last_verified_at=datetime.now(timezone.utc),
            )
        )
        report.bump("sales")
        return
    if existing.sales_count == sl["count"]:
        return
    old_source = db.get(Source, existing.source_id) if existing.source_id else None
    if _resolve(db, report, "series", series.id, f"monthly_sales.{sl['month']}", existing.sales_count, sl["count"], old_source, source):
        existing.sales_count = sl["count"]
        existing.source_id = source.id
        existing.last_verified_at = datetime.now(timezone.utc)
        report.bump_update("sales")
