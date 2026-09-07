"""SKU 对比接口。

- POST /comparisons          创建对比（1～5 个 SKU；同内容幂等复用）
- GET  /comparisons/{id}     读取对比：参数分组 + 隐藏相同参数（common_params）
游客分享使用 URL 编码无状态方案；本接口服务于保存的对比与前端取数。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import services as catalog
from app.common.database import get_session
from app.common.enums import COMPARISON_MAX_VARIANTS, MISSING_VALUE_LABEL
from app.common.errors import bad_request, not_found
from app.common.models import Brand, Comparison, ComparisonItem, VehicleSeries, VehicleVariant
from app.comparison.schemas import (
    CommonParamOut,
    ComparisonCreate,
    ComparisonDetailOut,
    ComparisonFactOut,
    ComparisonSummaryOut,
    CompareVariantOut,
)
from app.variants.normalization import display_fact_value, fact_display_label, fact_identity

router = APIRouter(tags=["comparison"])


@router.post("/comparisons", response_model=ComparisonSummaryOut, status_code=201)
def create_comparison(payload: ComparisonCreate, db: Session = Depends(get_session)) -> ComparisonSummaryOut:
    variant_ids = list(dict.fromkeys(payload.variant_ids))  # 去重保序
    if not variant_ids:
        raise bad_request("至少需要 1 个 SKU")
    if len(variant_ids) > COMPARISON_MAX_VARIANTS:
        raise bad_request(f"最多同时对比 {COMPARISON_MAX_VARIANTS} 个 SKU")

    variants = db.scalars(select(VehicleVariant).where(VehicleVariant.id.in_(variant_ids))).all()
    found = {v.id: v for v in variants}
    missing = [vid for vid in variant_ids if vid not in found]
    if missing:
        raise not_found(f"SKU 不存在：{', '.join(map(str, missing))}")
    off_sale = [v.id for v in variants if v.status != "on_sale"]
    if off_sale:
        raise bad_request(f"SKU 已停售，不能加入对比：{', '.join(map(str, off_sale))}")

    # 内容幂等（评审 P1）：同一组 SKU 的重复创建（分享链接反复打开）复用已有行
    content_hash = hashlib.sha256(",".join(map(str, sorted(variant_ids))).encode()).hexdigest()
    existing = db.scalar(
        select(Comparison)
        .where(Comparison.content_hash == content_hash, Comparison.owner_id.is_(None))
        .order_by(Comparison.id.desc())
    )
    if existing is not None:
        existing_ids = [item.variant_id for item in sorted(existing.items, key=lambda i: i.position)]
        return ComparisonSummaryOut(
            id=existing.id,
            variant_ids=existing_ids,
            created_at=existing.created_at,
        )

    comparison = Comparison(content_hash=content_hash)
    for position, vid in enumerate(variant_ids):
        comparison.items.append(ComparisonItem(variant_id=vid, position=position))
    db.add(comparison)
    db.commit()
    db.refresh(comparison)
    return ComparisonSummaryOut(
        id=comparison.id,
        variant_ids=variant_ids,
        created_at=comparison.created_at,
    )


@router.get("/comparisons/{comparison_id}", response_model=ComparisonDetailOut)
def get_comparison(comparison_id: int, db: Session = Depends(get_session)) -> ComparisonDetailOut:
    comparison = db.get(Comparison, comparison_id)
    if comparison is None:
        raise not_found(f"对比不存在：{comparison_id}")

    items = sorted(comparison.items, key=lambda i: i.position)
    variants: list[CompareVariantOut] = []
    for item in items:
        variant = db.get(VehicleVariant, item.variant_id)
        if variant is None or variant.status != "on_sale":
            continue  # 已停售/删除的 SKU 不再展示
        series = db.get(VehicleSeries, variant.series_id)
        brand = db.get(Brand, series.brand_id) if series else None
        price = catalog.variant_current_price(db, variant.id)
        facts = []
        for f in catalog.variant_facts(db, variant.id):
            raw = f.fact_value
            value = raw or MISSING_VALUE_LABEL
            display = display_fact_value(raw, f.unit, f.cycle) or MISSING_VALUE_LABEL
            facts.append(
                ComparisonFactOut(
                    category=f.category,
                    fact_key=f.fact_key,
                    label=fact_display_label(f.fact_key, f.unit, f.cycle),
                    value=value,
                    unit=f.unit,
                    cycle=f.cycle,
                    display=display,
                )
            )
        variants.append(
            CompareVariantOut(
                variant_id=variant.id,
                series_id=variant.series_id,
                series_name=series.name if series else "",
                brand_name=brand.name if brand else "",
                display_name=variant.display_name,
                energy_type=variant.energy_type,
                body_type=variant.body_type,
                price_cny=float(price.price_cny) if price else None,
                official_page_url=series.official_page_url if series else None,
                facts=facts,
            )
        )

    # 隐藏相同参数：所有参与对比的 SKU 归一化后完全一致的事实
    value_map: dict[tuple[str, str], list[tuple[str, str, str, str, str]]] = defaultdict(list)
    display_map: dict[tuple[str, str], str] = {}
    for v in variants:
        for f in v.facts:
            identity = fact_identity(f.category, f.fact_key, f.value, f.unit, f.cycle)
            key = (f.category, f.fact_key)
            value_map[key].append(identity)
            display_map.setdefault(key, f.display)

    common_params = [
        CommonParamOut(category=key[0], fact_key=key[1], display=display_map[key])
        for key, identities in value_map.items()
        if len(identities) == len(variants) and len(set(identities)) == 1
    ]
    common_params.sort(key=lambda p: (p.category, p.fact_key))

    return ComparisonDetailOut(
        id=comparison.id,
        # 只返回实际参与展示的 SKU（评审 L7：与 variants 列表保持一致）
        variant_ids=[v.variant_id for v in variants],
        created_at=comparison.created_at,
        variants=variants,
        common_params=common_params,
    )
