"""管理后台接口测试（PATCH 状态、不删除历史数据）。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.common.models import DataQualityConflict
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

ADMIN = {"Authorization": "Bearer test-admin-token"}


def _seed(db_session: Session):
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="后台测试品牌", source=source)
    series = make_series(db_session, brand, name="后台测试车系", source=source)
    year = make_year(db_session, series)
    variant = make_variant(db_session, series, year, config_version="标准版", price_cny="129800", source=source)
    db_session.add(
        DataQualityConflict(
            entity_type="variant",
            entity_id=variant.id,
            field="official_price",
            value_a="129800",
            value_b="99999",
            source_a_id=source.id,
            source_b_id=source.id,
            status="open",
        )
    )
    db_session.commit()
    return brand.id, series.id, variant.id


def test_admin_auth_required(client: TestClient, db_session: Session):
    _seed(db_session)
    assert client.get("/api/v1/admin/stats").status_code == 401  # 缺凭据
    assert client.get("/api/v1/admin/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_admin_unconfigured_rejected(client: TestClient, db_session: Session, monkeypatch):
    _seed(db_session)
    from app.common.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_token", "")  # 未配置
    assert client.get("/api/v1/admin/stats", headers=ADMIN).status_code == 503


def test_admin_brands_and_patch(client: TestClient, db_session: Session):
    brand_id, _, _ = _seed(db_session)

    brands = client.get("/api/v1/admin/brands", headers=ADMIN)
    assert brands.status_code == 200
    assert any(b["name"] == "后台测试品牌" for b in brands.json())

    patched = client.patch(
        f"/api/v1/admin/brands/{brand_id}",
        json={"active_status": "inactive", "inclusion_reason": "官方页面下架", "note": "品牌官网已下架该车型线"},
        headers=ADMIN,
    )
    assert patched.status_code == 200
    assert patched.json()["active_status"] == "inactive"

    # 评审 M10：手工编辑写入审计记录
    conflicts = client.get("/api/v1/admin/data-quality-conflicts", headers=ADMIN).json()
    audit = [c for c in conflicts if c["entity_type"] == "brand" and c["entity_id"] == brand_id
             and c["field"] == "active_status"]
    assert audit, "手工编辑应产生审计记录"
    assert audit[0]["resolution"] == "品牌官网已下架该车型线"

    # 状态变化不删除历史数据：仍可查到（含 inactive）
    brands = client.get("/api/v1/admin/brands", headers=ADMIN).json()
    target = next(b for b in brands if b["id"] == brand_id)
    assert target["active_status"] == "inactive"
    assert client.patch("/api/v1/admin/brands/999999", json={}, headers=ADMIN).status_code == 404


def test_admin_series_and_variant_patch(client: TestClient, db_session: Session):
    _, series_id, variant_id = _seed(db_session)

    assert client.patch(
        f"/api/v1/admin/series/{series_id}",
        json={"active_status": "inactive", "positioning": "停售车型"},
        headers=ADMIN,
    ).status_code == 200
    assert client.patch(
        f"/api/v1/admin/variants/{variant_id}",
        json={"status": "off_sale"},
        headers=ADMIN,
    ).json()["status"] == "off_sale"


def test_admin_conflicts_list_and_resolve(client: TestClient, db_session: Session):
    _seed(db_session)

    conflicts = client.get("/api/v1/admin/data-quality-conflicts", headers=ADMIN).json()
    assert len(conflicts) == 1
    conflict_id = conflicts[0]["id"]

    resolved = client.patch(
        f"/api/v1/admin/data-quality-conflicts/{conflict_id}",
        json={"resolution": "以官方来源价格为准"},
        headers=ADMIN,
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_at"] is not None

    open_list = client.get("/api/v1/admin/data-quality-conflicts?status=open", headers=ADMIN).json()
    assert open_list == []
    assert client.patch(
        "/api/v1/admin/data-quality-conflicts/999999", json={"resolution": "x"}, headers=ADMIN
    ).status_code == 404


def test_admin_stats(client: TestClient, db_session: Session):
    _seed(db_session)
    stats = client.get("/api/v1/admin/stats", headers=ADMIN).json()
    assert stats["brands"] == 1
    assert stats["series"] == 1
    assert stats["variants"] == 1
    assert stats["open_conflicts"] == 1
