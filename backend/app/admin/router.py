"""管理后台接口（独立凭据，PATCH 状态而非删除）。"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin.schemas import (
    AdminStats,
    BrandAdminOut,
    BrandPatch,
    ConflictOut,
    ConflictResolve,
    SeriesPatch,
    VariantPatch,
)
from app.common.config import get_settings
from app.common.database import get_session
from app.common.errors import not_found
from app.common.models import (
    Brand,
    DataQualityConflict,
    MonthlySales,
    OfficialPrice,
    SourceDocument,
    SpecFact,
    User,
    VehicleModelYear,
    VehicleSeries,
    VehicleVariant,
)

router = APIRouter(tags=["admin"])


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """管理后台独立凭据鉴权（Bearer token，经环境变量注入；不开放注册）。

    未配置 → 503；缺凭据/凭据无效 → 401。
    """
    from fastapi import HTTPException

    settings = get_settings()
    if not settings.admin_api_token:
        raise HTTPException(status_code=503, detail="管理后台未配置（ADMIN_API_TOKEN 为空）。")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少管理凭据。")
    token = authorization[len("Bearer "):].strip()
    if not secrets.compare_digest(token, settings.admin_api_token):
        raise HTTPException(status_code=401, detail="管理凭据无效。")


@router.get("/admin/brands", response_model=list[BrandAdminOut], dependencies=[Depends(require_admin)])
def admin_brands(include_inactive: bool = True, db: Session = Depends(get_session)) -> list[BrandAdminOut]:
    stmt = select(Brand).order_by(Brand.id)
    if not include_inactive:
        stmt = stmt.where(Brand.active_status == "active")
    brands = db.scalars(stmt).all()
    return [
        BrandAdminOut(
            id=b.id,
            name=b.name,
            brand_type=b.brand_type,
            active_status=b.active_status,
            official_site=b.official_site,
            inclusion_reason=b.inclusion_reason,
            last_verified_at=b.last_verified_at,
        )
        for b in brands
    ]


def _audit_admin_edit(db: Session, entity_type: str, entity_id: int, changes: dict[str, tuple[str | None, str | None]],
                      note: str | None = None) -> None:
    """管理后台手工编辑审计（评审 M10）：变更写入质量冲突表，resolution 记录操作说明。"""
    for field, (old, new) in changes.items():
        if old == new:
            continue
        db.add(
            DataQualityConflict(
                entity_type=entity_type,
                entity_id=entity_id,
                field=field,
                value_a=str(old)[:500] if old is not None else None,
                value_b=str(new)[:500] if new is not None else None,
                resolution=(note or "admin_edit")[:500] or None,
                status="resolved",
                resolved_at=datetime.now(timezone.utc),
            )
        )


@router.patch("/admin/brands/{brand_id}", response_model=BrandAdminOut, dependencies=[Depends(require_admin)])
def admin_patch_brand(brand_id: int, payload: BrandPatch, db: Session = Depends(get_session)) -> BrandAdminOut:
    brand = db.get(Brand, brand_id)
    if brand is None:
        raise not_found(f"品牌不存在：{brand_id}")
    changes: dict[str, tuple[str | None, str | None]] = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "note":
            continue  # note 是审计说明，不是实体字段
        changes[field] = (getattr(brand, field), value)
        setattr(brand, field, value)
    brand.last_verified_at = datetime.now(timezone.utc)
    _audit_admin_edit(db, "brand", brand.id, changes, payload.note)
    db.commit()
    db.refresh(brand)
    return BrandAdminOut(
        id=brand.id,
        name=brand.name,
        brand_type=brand.brand_type,
        active_status=brand.active_status,
        official_site=brand.official_site,
        inclusion_reason=brand.inclusion_reason,
        last_verified_at=brand.last_verified_at,
    )


@router.patch("/admin/series/{series_id}", dependencies=[Depends(require_admin)])
def admin_patch_series(series_id: int, payload: SeriesPatch, db: Session = Depends(get_session)) -> dict:
    series = db.get(VehicleSeries, series_id)
    if series is None:
        raise not_found(f"车型系列不存在：{series_id}")
    changes: dict[str, tuple[str | None, str | None]] = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "note":
            continue
        changes[field] = (getattr(series, field), value)
        setattr(series, field, value)
    series.last_verified_at = datetime.now(timezone.utc)
    _audit_admin_edit(db, "series", series.id, changes, payload.note)
    db.commit()
    return {"id": series.id, "active_status": series.active_status}


@router.patch("/admin/variants/{variant_id}", dependencies=[Depends(require_admin)])
def admin_patch_variant(variant_id: int, payload: VariantPatch, db: Session = Depends(get_session)) -> dict:
    variant = db.get(VehicleVariant, variant_id)
    if variant is None:
        raise not_found(f"SKU 不存在：{variant_id}")
    if payload.status is not None:
        _audit_admin_edit(db, "variant", variant.id, {"status": (variant.status, payload.status)}, payload.note)
        variant.status = payload.status
        variant.last_verified_at = datetime.now(timezone.utc)
        db.commit()
    return {"id": variant.id, "status": variant.status}


@router.get("/admin/data-quality-conflicts", response_model=list[ConflictOut], dependencies=[Depends(require_admin)])
def admin_conflicts(status: str | None = None, db: Session = Depends(get_session)) -> list[ConflictOut]:
    stmt = select(DataQualityConflict).order_by(DataQualityConflict.created_at.desc())
    if status:
        stmt = stmt.where(DataQualityConflict.status == status)
    rows = db.scalars(stmt).all()
    return [
        ConflictOut(
            id=c.id,
            entity_type=c.entity_type,
            entity_id=c.entity_id,
            field=c.field,
            value_a=c.value_a,
            value_b=c.value_b,
            source_a_id=c.source_a_id,
            source_b_id=c.source_b_id,
            status=c.status,
            resolution=c.resolution,
            created_at=c.created_at,
            resolved_at=c.resolved_at,
        )
        for c in rows
    ]


@router.patch("/admin/data-quality-conflicts/{conflict_id}", response_model=ConflictOut, dependencies=[Depends(require_admin)])
def admin_resolve_conflict(
    conflict_id: int, payload: ConflictResolve, db: Session = Depends(get_session)
) -> ConflictOut:
    conflict = db.get(DataQualityConflict, conflict_id)
    if conflict is None:
        raise not_found(f"冲突记录不存在：{conflict_id}")
    conflict.status = "resolved"
    conflict.resolution = payload.resolution
    conflict.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conflict)
    return ConflictOut(
        id=conflict.id,
        entity_type=conflict.entity_type,
        entity_id=conflict.entity_id,
        field=conflict.field,
        value_a=conflict.value_a,
        value_b=conflict.value_b,
        source_a_id=conflict.source_a_id,
        source_b_id=conflict.source_b_id,
        status=conflict.status,
        resolution=conflict.resolution,
        created_at=conflict.created_at,
        resolved_at=conflict.resolved_at,
    )


@router.get("/admin/stats", response_model=AdminStats, dependencies=[Depends(require_admin)])
def admin_stats(db: Session = Depends(get_session)) -> AdminStats:
    def count(model) -> int:
        return db.scalar(select(func.count()).select_from(model)) or 0

    return AdminStats(
        brands=count(Brand),
        series=count(VehicleSeries),
        model_years=count(VehicleModelYear),
        variants=count(VehicleVariant),
        prices=count(OfficialPrice),
        facts=count(SpecFact),
        sales=count(MonthlySales),
        source_documents=count(SourceDocument),
        open_conflicts=db.scalar(
            select(func.count()).select_from(DataQualityConflict).where(DataQualityConflict.status == "open")
        )
        or 0,
        users=count(User),
    )
