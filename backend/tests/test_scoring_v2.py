"""评分 v2（七维、全部同价位锚点）的契约测试。

覆盖四层：
1. 纯函数：价格带、权重归一、缺数据中性、coverage、成本/动力/配置特征；
2. 锚点文件：十档价格带、七维子树、版本号；
3. 内核：候选集与 v1 等价、确定性、多样性、无软信息按销量；
4. 开关：off 逐位回 v1、new 返回 v2、shadow 只记录不改变返回值、非法值回退。
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent import scoring_v2
from app.agent.schemas import UserProfile
from app.agent.weights import SCORING_V2_DIM_KEYS
from tests.seed import (
    make_brand,
    make_sales,
    make_series,
    make_source,
    make_variant,
    make_year,
)

# 与 scoring_v2.SAFETY_GROUP_PATTERNS 的 10 组一一对应，便于用 safety_count 精确控制命中组数。
SAFETY_KEYS = (
    "主/副驾驶座安全气囊",
    "前排侧气囊",
    "前/后排头部气囊(气帘)",
    "胎压监测",
    "车身稳定控制(ESC/ESP/DSC等)",
    "主动刹车/主动安全系统",
    "前方碰撞预警",
    "车道偏离预警",
    "并线辅助",
    "ISOFIX儿童座椅接口",
)
COMFORT_KEYS = ("内部配置", "外部配置", "操控配置")
INTELLIGENCE_KEYS = (
    "车道偏离预警系统",
    "并线辅助",
    "自适应巡航",
    "车道保持辅助系统",
)


def _facts(
    *,
    wheelbase: str = "2750",
    length: str = "4600",
    width: str = "1850",
    height: str = "1650",
    curb: str = "1800",
    power_kw: str = "160",
    fuel: str | None = None,
    electric: str | None = None,
    range_km: str | None = None,
    seats: str = "5",
    safety_count: int = 9,
    comfort_count: int = 8,
    intelligence_count: int = 3,
) -> list[tuple[str, str, str, str | None, str | None]]:
    facts: list[tuple[str, str, str, str | None, str | None]] = [
        ("参数信息", "轴距(mm)", wheelbase, "mm", None),
        ("参数信息", "长*宽*高(mm)", f"{length}*{width}*{height}", "mm", None),
        ("参数信息", "整备质量(kg)", curb, "kg", None),
        ("参数信息", "电动机总功率(kW)", power_kw, "kW", None),
        ("参数信息", "座位数(个)", seats, "座", None),
    ]
    if fuel is not None:
        facts.append(("参数信息", "油耗(L/100km)", fuel, "L/100km", "WLTC"))
    if electric is not None:
        facts.append(("参数信息", "百公里耗电量(kWh/100km)", electric, "kWh/100km", "CLTC"))
    if range_km is not None:
        facts.append(("参数信息", "续航里程(km)", range_km, "km", "CLTC"))
    for idx in range(safety_count):
        facts.append(("安全配置", SAFETY_KEYS[idx % len(SAFETY_KEYS)], "●", None, None))
    # 一个明确的 ○：不应被算作标配
    facts.append(("安全配置", "夜视系统", "○", None, None))
    for idx in range(comfort_count):
        category = COMFORT_KEYS[idx % len(COMFORT_KEYS)]
        facts.append((category, f"舒适项{idx}", "●", None, None))
    for idx in range(intelligence_count):
        facts.append(("智能/辅助驾驶", INTELLIGENCE_KEYS[idx], "●", None, None))
    return facts


def _seed_v2(db: Session) -> dict[str, int]:
    source = make_source(db, name="v2 测试来源")
    brand = make_brand(db, name="v2 品牌", source=source)
    suv = make_series(db, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    sedan = make_series(db, brand, name="通勤家轿", body_type="sedan", energy_types=("ICE",), source=source)
    lux = make_series(db, brand, name="豪华SUV", body_type="suv", energy_types=("BEV",), source=source)
    phev = make_series(db, brand, name="混动轿车", body_type="sedan", energy_types=("PHEV",), source=source)
    sparse = make_series(db, brand, name="极简小车", body_type="sedan", energy_types=("BEV",), source=source)
    y1 = make_year(db, suv)
    y2 = make_year(db, sedan)
    y3 = make_year(db, lux)
    y4 = make_year(db, phev)
    y5 = make_year(db, sparse)
    ids = {}
    v_suv = make_variant(
        db, suv, y1, config_version="标准版", energy_type="BEV", price_cny="110000",
        facts=_facts(electric="13.0", range_km="520", safety_count=9, comfort_count=8),
        source=source,
    )
    v_suv2 = make_variant(
        db, suv, y1, config_version="高配版", energy_type="BEV", price_cny="112000",
        facts=_facts(electric="12.5", range_km="540", safety_count=10, comfort_count=12),
        source=source,
    )
    v_sedan = make_variant(
        db, sedan, y2, config_version="标准版", energy_type="ICE", price_cny="115000",
        facts=_facts(
            wheelbase="2700", length="4700", curb="1450", power_kw="130",
            fuel="6.2", safety_count=8, comfort_count=7,
        ),
        source=source,
    )
    v_lux = make_variant(
        db, lux, y3, config_version="豪华版", energy_type="BEV", price_cny="145000",
        facts=_facts(electric="14.0", range_km="600", safety_count=10, comfort_count=15),
        source=source,
    )
    v_phev = make_variant(
        db, phev, y4, config_version="混动版", energy_type="PHEV", price_cny="135000",
        facts=_facts(
            wheelbase="2800", length="4800", curb="1900", power_kw="200",
            fuel="5.0", electric="18.0", range_km="120", safety_count=9, comfort_count=10,
        ),
        source=source,
    )
    v_sparse = make_variant(
        db, sparse, y5, config_version="基础版", energy_type="BEV", price_cny="105000",
        facts=None, source=source,
    )
    for series, count in ((suv, 300), (sedan, 800), (lux, 100), (phev, 200), (sparse, 900)):
        make_sales(db, series, "2026-09", count, source=source)
    db.commit()
    ids.update(
        suv=v_suv.id, suv2=v_suv2.id, sedan=v_sedan.id, lux=v_lux.id,
        phev=v_phev.id, sparse=v_sparse.id,
    )
    return ids


# ── 1. 纯函数 ────────────────────────────────────────────────────────────────
def test_price_band_boundaries():
    assert scoring_v2.price_band_index(None) is None
    assert scoring_v2.price_band_index(79999) == 0
    assert scoring_v2.price_band_index(80000) == 1
    assert scoring_v2.price_band_index(119999) == 2
    assert scoring_v2.price_band_index(120000) == 3
    assert scoring_v2.price_band_index(10_000_000) == 9


def test_effective_weights_usage_activation_and_aliases():
    inactive = scoring_v2.effective_weights({}, usage_active=False)
    assert "usage" not in inactive
    assert set(inactive) == set(SCORING_V2_DIM_KEYS) - {"usage"}
    assert abs(sum(inactive.values()) - 1.0) < 1e-9
    assert inactive["safety"] > inactive["running_cost"] > inactive["space"]

    active = scoring_v2.effective_weights({}, usage_active=True)
    assert set(active) == set(SCORING_V2_DIM_KEYS)
    assert abs(sum(active.values()) - 1.0) < 1e-9

    overridden = scoring_v2.effective_weights(
        {"safety": 1.0, "space": 0.5, "energy": 0.25, "budget": 5.0, "maintenance": 5.0},
        usage_active=False,
    )
    assert "budget" not in overridden and "maintenance" not in overridden
    assert "running_cost" in overridden, "v1 的 energy 键必须映射到 running_cost"
    assert overridden["safety"] > overridden["space"] > overridden["running_cost"]
    assert abs(sum(overridden.values()) - 1.0) < 1e-9


def test_aggregate_missing_data_is_neutral_and_coverage_is_disclosed():
    result = scoring_v2.aggregate(
        {"space": 1.0, "power": 0.0},
        {"space": True},
        {"space": 0.5, "power": 0.5},
    )
    assert result["score"] == pytest.approx(0.75)
    assert result["coverage"] == pytest.approx(0.5)
    assert result["low_evidence"] is False
    assert result["parts"]["power"] == pytest.approx(0.25)

    empty = scoring_v2.aggregate({}, {}, {"space": 1.0})
    assert empty["score"] == pytest.approx(0.5)
    assert empty["coverage"] == 0.0
    assert empty["low_evidence"] is True


def test_extract_features_cost_power_and_config_counts():
    variant = type("V", (), {"energy_type": "PHEV", "body_type": "sedan", "seat_count": 5})()
    facts = [
        {"category": "参数信息", "fact_key": "油耗(L/100km)", "value": "5.0", "unit": "L/100km", "cycle": "WLTC"},
        {"category": "参数信息", "fact_key": "百公里耗电量(kWh/100km)", "value": "18.0", "unit": "kWh/100km", "cycle": "CLTC"},
        {"category": "参数信息", "fact_key": "整备质量(kg)", "value": "1900", "unit": "kg", "cycle": None},
        {"category": "参数信息", "fact_key": "电动机总功率(kW)", "value": "150", "unit": "kW", "cycle": None},
        {"category": "参数信息", "fact_key": "系统综合功率(kW)", "value": "200", "unit": "kW", "cycle": None},
        {"category": "参数信息", "fact_key": "轴距(mm)", "value": "2800", "unit": "mm", "cycle": None},
        {"category": "参数信息", "fact_key": "长*宽*高(mm)", "value": "4800*1860*1500", "unit": "mm", "cycle": None},
        {"category": "安全配置", "fact_key": SAFETY_KEYS[0], "value": "●", "unit": None, "cycle": None},
        {"category": "安全配置", "fact_key": "夜视系统", "value": "○", "unit": None, "cycle": None},
        {"category": "内部配置", "fact_key": "座椅加热", "value": "●", "unit": None, "cycle": None},
    ]
    features = scoring_v2.extract_features(variant, facts, 135000)
    assert features["power"]["power_kw"] == pytest.approx(200.0), "系统综合功率优先于电动机总功率"
    assert features["power"]["kw_per_t"] == pytest.approx(200 / 1.9)
    assert features["running_cost"]["cny_per_100km"] == pytest.approx(
        0.5 * 5.0 * scoring_v2.FUEL_PRICE_CNY_PER_L
        + 0.5 * 18.0 * scoring_v2.ELECTRICITY_PRICE_CNY_PER_KWH
    )
    assert features["safety_count"] == 1, "只有 ● 计入，○ 不计"
    assert features["safety_fact_count"] == 2
    assert features["comfort_count"] == 1
    assert features["space"]["wheelbase"] == pytest.approx(2800.0)


def test_evaluate_no_facts_is_all_neutral():
    variant = type("V", (), {"energy_type": "BEV", "body_type": "suv", "seat_count": None})()
    result = scoring_v2.evaluate(scoring_v2.extract_features(variant, [], 120000), UserProfile())
    assert result["score"] == pytest.approx(0.5)
    assert result["coverage"] == 0.0
    assert result["low_evidence"] is True
    assert all(abs(value - 0.5) < 1e-9 for value in result["utilities"].values())


def test_anchor_file_has_ten_bands_and_all_seven_dimensions():
    anchors = scoring_v2.load_anchors()
    assert anchors is not None
    assert anchors["anchor_version"] == scoring_v2.ANCHOR_VERSION
    assert len(anchors["by_price_band"]) == len(scoring_v2.PRICE_BANDS_V2)
    for band in anchors["by_price_band"].values():
        inner = band["anchors"]
        assert {"space", "power", "running_cost", "safety", "comfort", "intelligence", "usage"} <= set(inner)
        assert set(inner["power"]) == {"kw_per_t"}
        assert set(inner["running_cost"]) == {"cny_per_100km"}
        assert set(inner["safety"]) == {"count"}


def test_sort_rows_and_diversify():
    rows = [
        (0.9, {"variant_id": 1, "series_id": 10, "coverage": 0.5}),
        (0.9, {"variant_id": 2, "series_id": 10, "coverage": 0.9}),
        (0.8, {"variant_id": 3, "series_id": 11, "coverage": 1.0}),
        (0.8, {"variant_id": 4, "series_id": 12, "coverage": 1.0}),
    ]
    ordered = scoring_v2.sort_rows(rows, {10: 5, 11: 1, 12: 2})
    assert [item["variant_id"] for _s, item in ordered] == [2, 1, 4, 3]
    selected, relaxed = scoring_v2.diversify(ordered, 3)
    assert [item["variant_id"] for _s, item in selected] == [2, 1, 4], (
        "同车系最多 2 款：series 10 取 2/1，series 12 取 4，series 11 被挤掉"
    )
    assert relaxed == 0


# ── 2. 开关与 API 契约 ──────────────────────────────────────────────────────
def test_off_mode_is_v1_by_default(client: TestClient, db_session: Session, monkeypatch):
    _seed_v2(db_session)
    monkeypatch.delenv("AGENT_SCORING_MODE", raising=False)
    body = client.post(
        "/api/v1/recommendations",
        json={"budget": {"min": 100000, "max": 150000}, "usage": ["通勤"], "limit": 5},
    ).json()
    assert body["scoring_version"] == "scoring-v1"
    assert body["anchor_version"] is None
    assert "budget" in body["weights_used"], "off 必须是旧八维权重"
    assert all(variant["coverage"] is None for variant in body["variants"])
    assert all(variant["low_evidence"] is False for variant in body["variants"])


def test_new_mode_returns_v2_shape(client: TestClient, db_session: Session, monkeypatch):
    _seed_v2(db_session)
    monkeypatch.setenv("AGENT_SCORING_MODE", "new")
    body = client.post(
        "/api/v1/recommendations",
        json={"budget": {"min": 100000, "max": 150000}, "usage": ["通勤"], "passengers": 5, "limit": 5},
    ).json()
    assert body["scoring_version"] == "scoring-v2"
    assert body["anchor_version"]
    assert body["usage_active"] is True
    assert set(body["weights_used"]) == set(SCORING_V2_DIM_KEYS)
    assert abs(sum(body["weights_used"].values()) - 1.0) < 1e-6
    assert body["variants"]
    assert body["count"] >= len(body["variants"])
    for variant in body["variants"]:
        assert 0.0 <= variant["coverage"] <= 1.0
        assert isinstance(variant["low_evidence"], bool)
    # 同车系最多 2 款、前 5 至少 3 车系（种子有 5 个车系，条件满足）
    series_ids = [variant["series_id"] for variant in body["variants"]]
    assert len(series_ids) <= 5
    assert max(series_ids.count(sid) for sid in set(series_ids)) <= 2
    assert len(set(series_ids)) >= 3, "前 5 至少覆盖 3 个车系"
    assert body["diversity_relaxed"] == 0


def test_candidate_set_equal_to_v1(client: TestClient, db_session: Session, monkeypatch):
    _seed_v2(db_session)
    payload = {"budget": {"min": 100000, "max": 150000}, "limit": 5}
    monkeypatch.setenv("AGENT_SCORING_MODE", "new")
    v2 = client.post("/api/v1/recommendations", json=payload).json()
    monkeypatch.setenv("AGENT_SCORING_MODE", "off")
    v1 = client.post("/api/v1/recommendations", json=payload).json()
    assert v2["count"] == v1["count"]
    assert v1["count"] == 6, "同预算区间内 6 款候选（v1/v2 共用硬约束）"
    # 候选池完全相同：两版的 top5 必须是同一候选集的子集
    allowed = {v["variant_id"] for v in v2["variants"]} | {v["variant_id"] for v in v1["variants"]}
    assert {v["variant_id"] for v in v1["variants"]} <= allowed
    assert {v["variant_id"] for v in v2["variants"]} <= allowed


def test_new_mode_is_deterministic(client: TestClient, db_session: Session, monkeypatch):
    _seed_v2(db_session)
    monkeypatch.setenv("AGENT_SCORING_MODE", "new")
    payload = {"budget": {"min": 100000, "max": 150000}, "usage": ["通勤"], "limit": 5}
    first = client.post("/api/v1/recommendations", json=payload).json()
    second = client.post("/api/v1/recommendations", json=payload).json()
    assert [(v["variant_id"], v["score"]) for v in first["variants"]] == [
        (v["variant_id"], v["score"]) for v in second["variants"]
    ]


def test_shadow_mode_returns_v1_and_logs_delta(
    client: TestClient, db_session: Session, monkeypatch, caplog
):
    _seed_v2(db_session)
    monkeypatch.setenv("AGENT_SCORING_MODE", "shadow")
    with caplog.at_level(logging.INFO, logger="app.agent.recommendation"):
        body = client.post(
            "/api/v1/recommendations",
            json={"budget": {"min": 100000, "max": 150000}, "usage": ["通勤"], "limit": 5},
        ).json()
    assert body["scoring_version"] == "scoring-v1", "shadow 必须返回 v1"
    shadow_rows = [r.getMessage() for r in caplog.records if "scoring_shadow " in r.getMessage()]
    assert shadow_rows, "shadow 必须留下一条对比日志"
    payload = json.loads(shadow_rows[0].split("scoring_shadow ", 1)[1])
    assert payload["v1_top5"] and payload["v2_top5"]
    assert 0.0 <= payload["overlap"] <= 1.0
    assert "v2_mean_coverage" in payload and "v2_low_evidence_rate" in payload


def test_unknown_mode_falls_back_to_v1(client: TestClient, db_session: Session, monkeypatch):
    _seed_v2(db_session)
    monkeypatch.setenv("AGENT_SCORING_MODE", "not-a-mode")
    body = client.post("/api/v1/recommendations", json={"limit": 3}).json()
    assert body["scoring_version"] == "scoring-v1"


def test_no_soft_info_still_sorted_by_sales(client: TestClient, db_session: Session, monkeypatch):
    _seed_v2(db_session)
    monkeypatch.setenv("AGENT_SCORING_MODE", "new")
    body = client.post(
        "/api/v1/recommendations",
        json={"budget": {"min": 100000, "max": 150000}, "limit": 5},
    ).json()
    assert body["variants"][0]["series_name"] == "极简小车", "无软信息时销量最高的排第一"
