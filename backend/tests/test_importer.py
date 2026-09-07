"""数据导入管线测试（校验/归一化/来源冲突）。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.models import DataQualityConflict, MonthlySales, OfficialPrice, Source, VehicleVariant
from app.sources.importer import import_catalog, validate_payload


def _payload(source_type: str = "official_site", price: int = 129800, power: str = "150", sales: int = 1000) -> dict:
    """测试专用导入载荷（只用于测试，见 tests 约定）。"""
    return {
        "source": {
            "name": f"测试来源-{source_type}",
            "source_type": source_type,
            "url": "https://example.com/data",
            "verified_status": "verified",
            "credibility": "high",
        },
        "brands": [{"name": "导入测试品牌", "brand_type": "domestic_nev", "official_site": "https://example.com"}],
        "series": [
            {
                "brand": "导入测试品牌",
                "name": "导入测试车系",
                "body_type": "suv",
                "energy_types": ["BEV"],
                "positioning": "测试定位",
                "official_page_url": "https://example.com/series",
                "model_years": [
                    {
                        "year_name": "2025款",
                        "variants": [
                            {
                                "config_version": "标准版",
                                "powertrain": "纯电",
                                "drivetrain": "后驱",
                                "energy_type": "BEV",
                                "price_cny": price,
                                "facts": [
                                    {"category": "动力", "fact_key": "power_kw", "value": power, "unit": "kW"},
                                    {"category": "电池和续航", "fact_key": "range_km", "value": "605", "unit": "km", "cycle": "CLTC"},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "sales": [{"series": "导入测试车系", "month": "2026-07", "sales_type": "retail", "count": sales}],
    }


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_import_creates_full_catalog(db_session: Session):
    report = import_catalog(db_session, _payload())
    assert report.ok
    assert _count(db_session, Source) == 1
    assert _count(db_session, VehicleVariant) == 1
    assert _count(db_session, OfficialPrice) == 1
    assert _count(db_session, MonthlySales) == 1
    assert report.created.get("variant") == 1
    assert report.conflicts == []


def test_import_idempotent(db_session: Session):
    import_catalog(db_session, _payload())
    report = import_catalog(db_session, _payload())
    assert report.ok
    assert _count(db_session, VehicleVariant) == 1  # SKU 归一化：不重复建档
    assert _count(db_session, OfficialPrice) == 1
    assert report.conflicts == []
    assert all(v == 0 for v in report.created.values())


def test_conflict_lower_rank_kept(db_session: Session):
    import_catalog(db_session, _payload(source_type="official_site", price=129800, power="150", sales=1000))
    report = import_catalog(db_session, _payload(source_type="industry_data", price=99999, power="99", sales=9999))

    assert report.ok
    assert report.conflicts, "低优先级来源的差异应记录冲突"
    assert _count(db_session, DataQualityConflict) >= 1

    variant = db_session.scalar(select(VehicleVariant))
    prices = db_session.scalars(select(OfficialPrice).where(OfficialPrice.variant_id == variant.id)).all()
    active = [p for p in prices if p.effective_to is None]
    assert len(active) == 1 and active[0].price_cny == 129800  # 保留官方来源价格
    sales = db_session.scalar(select(MonthlySales))
    assert sales.sales_count == 1000


def test_conflict_higher_rank_wins(db_session: Session):
    import_catalog(db_session, _payload(source_type="industry_data", price=129800, power="150"))
    report = import_catalog(db_session, _payload(source_type="official_site", price=139800, power="160"))

    assert report.ok
    variant = db_session.scalar(select(VehicleVariant))
    prices = db_session.scalars(select(OfficialPrice).where(OfficialPrice.variant_id == variant.id)).all()
    assert len(prices) == 2  # 旧价关闭 + 新价生效，形成历史
    active = [p for p in prices if p.effective_to is None]
    assert len(active) == 1 and active[0].price_cny == 139800


def test_validation_fails_without_writes(db_session: Session):
    payload = _payload()
    payload["series"][0]["model_years"][0]["variants"][0]["energy_type"] = "HYDROGEN"
    errors = validate_payload(payload)
    assert errors

    report = import_catalog(db_session, payload)
    assert not report.ok
    assert _count(db_session, VehicleVariant) == 0  # 校验失败不写库


def test_validation_rejects_bad_effective_from():
    payload = _payload()
    payload["series"][0]["model_years"][0]["variants"][0]["effective_from"] = "2025/01/01"
    errors = validate_payload(payload)
    assert any("effective_from" in e for e in errors)


def test_import_allows_missing_price(db_session: Session):
    payload = _payload()
    del payload["series"][0]["model_years"][0]["variants"][0]["price_cny"]
    report = import_catalog(db_session, payload)
    assert report.ok
    assert _count(db_session, VehicleVariant) == 1
    assert _count(db_session, OfficialPrice) == 0  # 无官方指导价时不建价格行
