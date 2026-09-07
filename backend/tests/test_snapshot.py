"""数据快照导出/回滚测试（数据变更可追溯、可回滚）。"""
from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.common import models  # noqa: F401
from app.common.database import Base
from app.sources.importer import import_catalog
from app.sources.snapshot import export_payload
from tests.seed import make_brand, make_sales, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> None:
    source = make_source(db, name="官方测试来源")
    brand = make_brand(db, name="快照测试品牌", source=source)
    series = make_series(db, brand, name="快照测试车系", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db, series, "2025款")
    make_variant(
        db, series, year, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[("动力", "power_kw", "150", "kW", None), ("座位数", "seats", "5", "座", None)],
        source=source,
    )
    make_sales(db, series, "2026-07", 12345, source=source)
    db.commit()


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def _fresh_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def test_snapshot_roundtrip(db_session: Session):
    _seed(db_session)
    payload = export_payload(db_session)

    # 快照结构与 importer 载荷兼容
    assert payload["source"]["name"] == "快照导出（数据快照）"
    assert len(payload["brands"]) == 1
    assert len(payload["series"]) == 1
    variants = payload["series"][0]["model_years"][0]["variants"]
    assert len(variants) == 1
    assert variants[0]["price_cny"] == 129800.0
    assert len(variants[0]["facts"]) == 2
    assert payload["sales"][0]["count"] == 12345

    # 回滚/恢复：导入到全新库，行数一致
    target = _fresh_session()
    try:
        report = import_catalog(target, payload)
        assert report.ok, report.errors
        assert _count(target, models.VehicleVariant) == 1
        assert _count(target, models.OfficialPrice) == 1
        assert _count(target, models.SpecFact) == 2
        assert _count(target, models.MonthlySales) == 1
    finally:
        target.close()


def test_snapshot_roundtrip_without_price(db_session: Session):
    """无官方指导价的 SKU 也能快照并回滚（价格缺失不阻断导入）。"""
    from app.common.models import VehicleVariant

    _seed(db_session)
    variant = db_session.scalar(select(VehicleVariant))
    prices = db_session.scalars(
        select(models.OfficialPrice).where(models.OfficialPrice.variant_id == variant.id)
    ).all()
    for price in prices:
        db_session.delete(price)
    db_session.commit()

    payload = export_payload(db_session)
    assert payload["series"][0]["model_years"][0]["variants"][0]["price_cny"] is None

    target = _fresh_session()
    try:
        report = import_catalog(target, payload)
        assert report.ok, report.errors
        assert _count(target, models.VehicleVariant) == 1
        assert _count(target, models.OfficialPrice) == 0
    finally:
        target.close()
