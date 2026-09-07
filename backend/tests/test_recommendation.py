"""确定性推荐接口测试（§15.2 POST /api/v1/recommendations，硬约束 + 软评分）。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> dict[str, int]:
    """三款车：家用SUV 12.98万(BEV) / 通勤家轿 9.98万(BEV) / 燃油轿车 17.98万(ICE)。"""
    source = make_source(db, name="官方测试来源")
    brand = make_brand(db, name="测试品牌", source=source)
    suv = make_series(db, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    sedan = make_series(db, brand, name="通勤家轿", body_type="sedan", energy_types=("BEV",), source=source)
    ice = make_series(db, brand, name="燃油轿车", body_type="sedan", energy_types=("ICE",), source=source)
    y1, y2, y3 = make_year(db, suv), make_year(db, sedan), make_year(db, ice)
    v_suv = make_variant(
        db, suv, y1, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[("座位数", "座位数(个)", "5", "座", None), ("动力", "电动机总功率(kW)", "150", "kW", None)],
        source=source,
    )
    v_sedan = make_variant(
        db, sedan, y2, config_version="标准版", energy_type="BEV", price_cny="99800",
        facts=[("座位数", "座位数(个)", "5", "座", None), ("动力", "电动机总功率(kW)", "120", "kW", None)],
        source=source,
    )
    v_ice = make_variant(
        db, ice, y3, config_version="豪华版", energy_type="ICE", price_cny="179800",
        facts=[("动力", "电动机总功率(kW)", "137", "kW", None)], source=source,
    )
    db.commit()
    return {"suv": v_suv.id, "sedan": v_sedan.id, "ice": v_ice.id}


def test_recommendations_hard_filters_and_shape(client: TestClient, db_session: Session):
    ids = _seed(db_session)
    resp = client.post(
        "/api/v1/recommendations",
        json={"budget": {"min": 90000, "max": 130000}, "usage": ["通勤"], "passengers": 2, "limit": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["variants"], "预算 9~13 万内应有候选"
    got = {v["variant_id"] for v in body["variants"]}
    assert got <= {ids["suv"], ids["sedan"]}, "超出预算的燃油轿车（17.98万）应被硬约束排除"
    assert all(v["price_cny"] <= 130000 for v in body["variants"])
    first = body["variants"][0]
    for key in ("score", "matched", "display_name", "series_name", "official_page_url"):
        assert key in first
    assert set(body["weights_used"]) >= {"budget", "space", "power"}
    assert body["count"] >= len(body["variants"])
    # §15.3：响应应为纯数据（无内部对象泄漏）
    assert not any("_sa_" in str(k) for k in body)


def test_recommendations_weights_override(client: TestClient, db_session: Session):
    _seed(db_session)
    resp = client.post(
        "/api/v1/recommendations",
        json={"budget": {"min": 90000, "max": 200000}, "weights": {"space": 1.0, "power": 0.0}, "limit": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["weights_used"]["space"] == 1.0
    assert body["weights_used"]["power"] == 0.0
    assert body["weights_used"]["budget"] == 0.3, "未覆盖的维度保持默认权重"


def test_recommendations_limit_and_validation(client: TestClient, db_session: Session):
    _seed(db_session)
    assert client.post("/api/v1/recommendations", json={"limit": 0}).status_code == 422
    assert client.post("/api/v1/recommendations", json={"limit": 11}).status_code == 422
    # 无画像 = 全量软评分但预算中性，不报错
    resp = client.post("/api/v1/recommendations", json={})
    assert resp.status_code == 200
    assert resp.json()["variants"], "无约束时返回中性排序候选"
