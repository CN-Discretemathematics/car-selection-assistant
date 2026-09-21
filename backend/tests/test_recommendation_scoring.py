# -*- coding: utf-8 -*-
"""推荐软评分 8 维语义逐维单测（§17.2：输入事实 → 维度得分）。

设计原则（防「迎合答案」）：
- 全部走公开 `recommendation_tool`，不触评分内部实现；
- 每条用例构造**只在一个维度上有差异**的两/三款车，其余维度完全相同——
  分差 = weights_used[维度] × Δ维度 / Σweights，其中 weights_used 是工具的
  公开返回契约，期望值由它推导而非硬编码权重数字（不镜像实现公式）；
- 同时钉住「无信息 → 中性 0.5」与 matched 文案等对外语义。

种子形状沿用既有测试（tests/seed.py）：variant.body_type 继承自 series；
facts 形状与真实 autohome 导入一致（类目/键/值/单位/周期）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.schemas import UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_sales, make_series, make_source, make_variant, make_year


def _seed(db: Session, specs: list[dict]) -> list[int]:
    """specs 每项：price / body_type / energy_type / facts / sales。返回 variant id 列表。"""
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)
    ids: list[int] = []
    for i, spec in enumerate(specs):
        series = make_series(
            db, brand, name=f"车系{i}", body_type=spec.get("body_type", "sedan"),
            energy_types=(spec.get("energy_type", "BEV"),), source=source,
        )
        year = make_year(db, series)
        variant = make_variant(
            db, series, year, config_version=spec.get("config", "标准版"),
            energy_type=spec.get("energy_type", "BEV"),
            price_cny=spec.get("price", "150000"), facts=spec.get("facts"), source=source,
        )
        if spec.get("sales"):
            make_sales(db, series, "2026-08", 10000, source=source)
        ids.append(variant.id)
    db.commit()
    return ids


def _run(db: Session, profile: UserProfile | None = None) -> dict:
    return recommendation_tool(db, profile or UserProfile(), limit=10)


def _score_of(result: dict, variant_id: int) -> float:
    for v in result["variants"]:
        if v["variant_id"] == variant_id:
            return v["score"]
    raise AssertionError(f"variant {variant_id} 不在推荐结果里")


def _delta(w: dict, dim: str, dim_a: float, dim_b: float) -> float:
    """公开契约推导：两车只差 dim 维度（Δ=dim_a-dim_b）时的期望分差。"""
    return w[dim] * (dim_a - dim_b) / sum(w.values())


# ── budget 维度 ──────────────────────────────────────────────────────────────

def test_budget_range_mid_closeness(db_session: Session):
    """预算区间：贴近区间中点得分高（dim 1.0 vs 0.6），且只有 ≥0.8 才给「预算匹配」。"""
    ids = _seed(db_session, [{"price": "150000"}, {"price": "190000"}])
    profile = UserProfile()
    profile.budget.min, profile.budget.max = 100000, 200000
    result = _run(db_session, profile)
    w = result["weights_used"]
    a, b = _score_of(result, ids[0]), _score_of(result, ids[1])
    assert a > b
    assert a - b == pytest.approx(_delta(w, "budget", 1.0, 0.6), abs=3e-4)
    matched_a = next(v["matched"] for v in result["variants"] if v["variant_id"] == ids[0])
    matched_b = next(v["matched"] for v in result["variants"] if v["variant_id"] == ids[1])
    assert "预算匹配" in matched_a and "预算匹配" not in matched_b


def test_budget_min_only_is_neutral(db_session: Session):
    """只报预算下限：候选间不产生预算分差（中性 0.5），也不给「预算匹配」。"""
    ids = _seed(db_session, [{"price": "110000"}, {"price": "180000"}])
    profile = UserProfile()
    profile.budget.min = 100000
    result = _run(db_session, profile)
    assert _score_of(result, ids[0]) == _score_of(result, ids[1])
    assert all("预算匹配" not in v["matched"] for v in result["variants"])


def test_budget_max_only_all_match(db_session: Session):
    """只报预算上限：硬过滤后 survivors 全部 ≤上限 → 预算维恒 1.0、全部「预算匹配」。"""
    ids = _seed(db_session, [{"price": "150000"}, {"price": "120000"}])
    profile = UserProfile()
    profile.budget.max = 150000
    result = _run(db_session, profile)
    assert _score_of(result, ids[0]) == _score_of(result, ids[1])
    assert all("预算匹配" in v["matched"] for v in result["variants"])


# ── usage 维度 ───────────────────────────────────────────────────────────────

def test_usage_commute_prefers_sedan(db_session: Session):
    """「通勤」命中轿车车身（dim 1.0 vs 0.0），分差 = weights_used['usage']。"""
    ids = _seed(db_session, [{"body_type": "sedan"}, {"body_type": "suv"}])
    profile = UserProfile()
    profile.usage = ["通勤"]
    result = _run(db_session, profile)
    w = result["weights_used"]
    a, b = _score_of(result, ids[0]), _score_of(result, ids[1])
    assert a > b
    assert a - b == pytest.approx(_delta(w, "usage", 1.0, 0.0), abs=3e-4)


def test_usage_empty_is_neutral(db_session: Session):
    """未给用途：车身不产生分差（中性 0.5）。"""
    ids = _seed(db_session, [{"body_type": "sedan"}, {"body_type": "suv"}])
    result = _run(db_session)
    assert _score_of(result, ids[0]) == _score_of(result, ids[1])


def test_usage_multi_hit_phev_bonus(db_session: Session):
    """多用途下命中数可叠加：通勤+长途 时 插混轿车（2 命中=1.0）> 纯电轿车（1 命中=0.5）。"""
    ids = _seed(db_session, [{"energy_type": "PHEV"}, {"energy_type": "BEV"}])
    profile = UserProfile()
    profile.usage = ["通勤", "长途"]
    result = _run(db_session, profile)
    w = result["weights_used"]
    a, b = _score_of(result, ids[0]), _score_of(result, ids[1])
    assert a > b
    assert a - b == pytest.approx(_delta(w, "usage", 1.0, 0.5), abs=3e-4)


# ── space / power 维度（事实线性归一，缺事实中性 0.5）────────────────────────

def test_space_length_gradient(db_session: Session):
    """车长 5200/4750/4300 → dim 1.0/0.5/0.0；分差由 weights_used['space'] 推导。"""
    ids = _seed(db_session, [
        {"facts": [("尺寸", "length_mm", "5200", "mm", None)]},
        {"facts": [("尺寸", "length_mm", "4750", "mm", None)]},
        {"facts": [("尺寸", "length_mm", "4300", "mm", None)]},
    ])
    result = _run(db_session)
    w = result["weights_used"]
    hi, mid, lo = (_score_of(result, i) for i in ids)
    assert hi > mid > lo
    assert hi - lo == pytest.approx(_delta(w, "space", 1.0, 0.0), abs=3e-4)


def test_power_gradient_and_missing_is_neutral(db_session: Session):
    """功率 300/80kW → dim 1.0/0.0；无功率事实 = 中性 0.5，恰在两者之间。"""
    ids = _seed(db_session, [
        {"facts": [("动力", "电动机总功率(kW)", "300", "kW", None)]},
        {},
        {"facts": [("动力", "电动机总功率(kW)", "80", "kW", None)]},
    ])
    result = _run(db_session)
    w = result["weights_used"]
    hi, mid, lo = (_score_of(result, i) for i in ids)
    assert hi > mid > lo
    assert hi - lo == pytest.approx(_delta(w, "power", 1.0, 0.0), abs=3e-4)


# ── energy 维度 ──────────────────────────────────────────────────────────────

def test_energy_neutral_prefers_new_energy(db_session: Session):
    """无能源偏好：新能源 0.7 > 燃油 0.4，分差 = 0.3 × weights_used['energy']。"""
    ids = _seed(db_session, [{"energy_type": "BEV"}, {"energy_type": "ICE"}])
    result = _run(db_session)
    w = result["weights_used"]
    a, b = _score_of(result, ids[0]), _score_of(result, ids[1])
    assert a > b
    assert a - b == pytest.approx(_delta(w, "energy", 0.7, 0.4), abs=3e-4)


def test_energy_preference_lifts_dim_to_one(db_session: Session):
    """给了能源偏好后命中款型的 energy 维度抬升到 1.0（对比无偏好 +0.3）。"""
    ids = _seed(db_session, [{"energy_type": "BEV"}])
    base = _run(db_session)
    profile = UserProfile()
    profile.energy_preference = ["BEV"]
    with_pref = _run(db_session, profile)
    w = with_pref["weights_used"]
    lift = _score_of(with_pref, ids[0]) - _score_of(base, ids[0])
    assert lift == pytest.approx(_delta(w, "energy", 1.0, 0.7), abs=3e-4)
    assert "能源偏好匹配" in next(
        v["matched"] for v in with_pref["variants"] if v["variant_id"] == ids[0]
    )


# ── comfort / intelligence 维度（类目事实存在性）─────────────────────────────

def test_comfort_and_intelligence_fact_presence(db_session: Session):
    """有「舒适性」/「智能驾驶」类目事实 → 该维 1.0；无事实 → 0.0。"""
    ids = _seed(db_session, [
        {"facts": [("舒适性", "真皮座椅", "有", None, None)]},
        {},
        {"facts": [("智能驾驶", "高速领航辅助", "有", None, None)]},
    ])
    result = _run(db_session)
    w = result["weights_used"]
    comfort, plain, intel = (_score_of(result, i) for i in ids)
    assert comfort - plain == pytest.approx(_delta(w, "comfort", 1.0, 0.0), abs=3e-4)
    assert intel - plain == pytest.approx(_delta(w, "intelligence", 1.0, 0.0), abs=3e-4)


# ── maintenance 维度（品牌规模 + 渠道活跃度代理）────────────────────────────

def test_maintenance_brand_scale_and_channel_activity(db_session: Session):
    """维护便利性代理：在库车系多的品牌（scale 1.0 → 0.7）压过单车系品牌（0.46）；
    该品牌有月销量（渠道活跃 +0.3）后反超——两义项各自被分差钉住。"""
    source = make_source(db_session, name="汽车之家")
    big = make_brand(db_session, name="大厂", source=source)
    small = make_brand(db_session, name="小厂", source=source)
    big_ids: list[int] = []
    for i in range(5):
        series = make_series(db_session, big, name=f"大厂车系{i}", source=source)
        year = make_year(db_session, series)
        v = make_variant(db_session, series, year, energy_type="BEV",
                         price_cny="150000", source=source)
        big_ids.append(v.id)
    small_series = make_series(db_session, small, name="小厂车系", source=source)
    small_year = make_year(db_session, small_series)
    small_v = make_variant(db_session, small_series, small_year, energy_type="BEV",
                           price_cny="150000", source=source)
    db_session.commit()

    before = _run(db_session)
    w = before["weights_used"]
    big_min = min(_score_of(before, i) for i in big_ids)
    small_before = _score_of(before, small_v.id)
    assert small_before < big_min, "无销量的单车系品牌应排在多车系品牌之后"
    assert big_min - small_before == pytest.approx(
        _delta(w, "maintenance", 0.7, 0.46), abs=3e-4
    )

    make_sales(db_session, small_series, "2026-08", 10000, source=source)
    db_session.commit()
    after = _run(db_session)
    small_after = _score_of(after, small_v.id)
    assert small_after > big_min, "渠道活跃后应反超"
    # 小厂 brand_scale 只有 0.2：0.4 + 0.3×0.2 + 0.3×1 = 0.76（不是满分 1.0）
    assert small_after - big_min == pytest.approx(
        _delta(w, "maintenance", 0.76, 0.7), abs=3e-4
    )


# ── matched 文案与权重覆盖 ───────────────────────────────────────────────────

def test_seat_matched_string(db_session: Session):
    """座位数 ≥ 乘客数 → matched 含「座位满足（≥N 座）」。"""
    ids = _seed(db_session, [{"facts": [("座位数", "座位数(个)", "5", "座", None)]}])
    profile = UserProfile()
    profile.passengers = 5
    result = _run(db_session, profile)
    matched = next(v["matched"] for v in result["variants"] if v["variant_id"] == ids[0])
    assert "座位满足（≥5 座）" in matched


def test_weight_override_flips_ranking(db_session: Session):
    """权重覆盖改排序：默认权重下「舒适性 1.0」与「功率 1.0」打平（0.05 vs 0.10×0.5）；
    把 comfort 权重抬到 1.0 → 舒适车胜；把 power 抬到 1.0 → 功率车胜。"""
    ids = _seed(db_session, [
        {"facts": [("舒适性", "真皮座椅", "有", None, None)]},
        {"facts": [("动力", "电动机总功率(kW)", "300", "kW", None)]},
    ])
    default = _run(db_session)
    assert _score_of(default, ids[0]) == _score_of(default, ids[1]), "默认权重下恰为平分"

    comfort_first = UserProfile()
    comfort_first.weights = {"comfort": 1.0}
    assert _score_of(_run(db_session, comfort_first), ids[0]) > _score_of(
        _run(db_session, comfort_first), ids[1]
    )

    power_first = UserProfile()
    power_first.weights = {"power": 1.0}
    assert _score_of(_run(db_session, power_first), ids[0]) < _score_of(
        _run(db_session, power_first), ids[1]
    )
