"""管理后台接口测试（PATCH 状态、不删除历史数据；多标签凭据与审计）。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.common.admin_auth import (
    admin_credentials,
    parse_admin_tokens,
    resolve_admin_label,
)
from app.common.models import DataQualityConflict
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

ADMIN = {"Authorization": "Bearer test-admin-token"}
AUDIT_TMP = Path(__file__).resolve().parent / ".tmp"


def test_parse_admin_tokens():
    assert parse_admin_tokens("ryan:abc,nightly:def") == [("ryan", "abc"), ("nightly", "def")]
    # 空项 / 缺冒号 / 缺标签或 token 一律忽略，不影响其余凭据
    assert parse_admin_tokens(" ryan:abc , ,bad,onlylabel:,:onlytoken") == [("ryan", "abc")]
    assert parse_admin_tokens("") == []


def test_credentials_include_legacy_single_token(monkeypatch):
    """旧单值 ADMIN_API_TOKEN 仍生效（标签 legacy），且与多标签重复时不重复计入。"""
    from app.common.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_tokens", "ryan:shared,nightly:solo")
    monkeypatch.setattr(settings, "admin_api_token", "shared")
    assert admin_credentials() == [("ryan", "shared"), ("nightly", "solo")]

    monkeypatch.setattr(settings, "admin_api_token", "legacy-only")
    assert admin_credentials() == [("ryan", "shared"), ("nightly", "solo"), ("legacy", "legacy-only")]


def test_resolve_admin_label(monkeypatch):
    from app.common.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_tokens", "ryan:aaa,nightly:bbb")
    monkeypatch.setattr(settings, "admin_api_token", "")
    assert resolve_admin_label("Bearer aaa") == "ryan"
    assert resolve_admin_label("Bearer bbb") == "nightly"
    assert resolve_admin_label("Bearer ccc") is None
    assert resolve_admin_label("aaa") is None  # 缺 Bearer 前缀
    assert resolve_admin_label(None) is None


def test_multi_token_grants_access_and_audits(client: TestClient, db_session: Session, monkeypatch):
    """多标签凭据可用；每次管理请求（含被拒的）都写审计（标签 + 真实 IP + 状态）。"""
    _seed(db_session)
    from app.common import admin_auth
    from app.common.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_tokens", "ryan:ryan-token,nightly:nightly-token")
    monkeypatch.setattr(settings, "admin_api_token", "")

    audit_path = AUDIT_TMP / "admin-audit-test.log"
    audit_path.unlink(missing_ok=True)
    monkeypatch.setattr(admin_auth, "AUDIT_LOG_PATH", audit_path)

    # 两个标签都能过；未知凭据 401
    assert client.get("/api/v1/admin/stats", headers={"Authorization": "Bearer ryan-token"}).status_code == 200
    assert client.get("/api/v1/admin/stats", headers={"Authorization": "Bearer nightly-token"}).status_code == 200
    assert client.get("/api/v1/admin/stats", headers={"Authorization": "Bearer nope"}).status_code == 401

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3, "每次管理请求都应留下审计行（含 401 尝试）"
    assert "label=ryan" in lines[0] and "status=200" in lines[0]
    assert "label=nightly" in lines[1]
    assert "label=-" in lines[2] and "status=401" in lines[2], "未通过鉴权的尝试也要记录（label=-）"
    assert all("GET /api/v1/admin/stats" in line for line in lines)
    assert all("ip=" in line for line in lines)


def test_non_admin_paths_not_audited(client: TestClient, db_session: Session, monkeypatch):
    """公开路径不写管理审计（避免把整站流量灌进审计文件）。"""
    _seed(db_session)
    from app.common import admin_auth

    audit_path = AUDIT_TMP / "admin-audit-public.log"
    audit_path.unlink(missing_ok=True)
    monkeypatch.setattr(admin_auth, "AUDIT_LOG_PATH", audit_path)

    assert client.get("/api/v1/health").status_code == 200
    assert not audit_path.exists()


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
