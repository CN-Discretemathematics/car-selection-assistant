"""对比差异分析测试：只比较同单位/可换算的数据，缺数据如实标注，不做任何推测。"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.common.models import SpecFact
from app.comparison.analysis import analyze_comparison, render_analysis_text
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_pair(db: Session, *, second_fast_charge: str = "小时", second_fast_charge_value: str = "0.68"):
    """两个真实结构的款型：A 强动力/长轴距/贵，B 便宜/轻。"""
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)

    def build(name: str, price: str, power: str, wheelbase: str, mass: str, fast_key: str, fast_value: str):
        series = make_series(db, brand, name=name, energy_types=("BEV",), source=source)
        year = make_year(db, series)
        variant = make_variant(db, series, year, config_version="标准版", energy_type="BEV",
                              price_cny=price, source=source)
        for category, key, value, unit in (
            ("参数信息", "最大功率(kW)", power, "kW"),
            ("参数信息", "轴距(mm)", wheelbase, "mm"),
            ("参数信息", "整备质量(kg)", mass, "kg"),
            ("参数信息", f"电池快充时间({fast_key})", fast_value, None),
            ("参数信息", "CLTC纯电续航里程(km)", "500", "km"),
        ):
            db.add(SpecFact(variant_id=variant.id, category=category, fact_key=key,
                            fact_value=value, unit=unit, source_id=source.id))
        return variant

    a = build("甲车系", "200000", "200", "2900", "2000", "分钟", "25.4")
    b = build("乙车系", "180000", "150", "2760", "1800", second_fast_charge, second_fast_charge_value)
    db.commit()
    return a.id, b.id


def test_basic_analysis_finds_leaders_and_gaps(db_session: Session):
    a_id, b_id = _seed_pair(db_session)
    result = analyze_comparison(db_session, [a_id, b_id])
    assert "error" not in result
    by_key = {d["key"]: d for d in result["dimensions"]}

    power = by_key["power"]
    assert power["significant"] is True, "65kW 差距应超过 15kW 阈值"
    leader = next(v for v in power["values"] if v["leader"])
    assert leader["variant_id"] == a_id
    assert "高 50kW" in (power["gap"] or "")

    wheelbase = by_key["wheelbase"]
    assert wheelbase["values"][0]["display"].startswith("2900")
    # 价格：A 贵 2 万且超过 1 万阈值
    assert result["price"]["significant"] is True
    assert "贵 2.00 万元" in (result["price"]["gap"] or "")
    # 取舍句里两个款型都要出现，且不含编造数据
    assert len(result["tradeoffs"]) == 2
    assert any("落后" in t for t in result["tradeoffs"])


def test_unit_conversion_minutes_vs_hours(db_session: Session):
    """实测坑：同维度一个写「25.4 分钟」、另一个写「0.68 小时」——必须换算后比较。"""
    a_id, b_id = _seed_pair(db_session)
    result = analyze_comparison(db_session, [a_id, b_id])
    fast = next(d for d in result["dimensions"] if d["key"] == "fast_charge")
    displays = [v["display"] for v in fast["values"]]
    assert any("≈ 40.8分钟" in d for d in displays), displays
    assert "40.8" in (fast["gap"] or "") or "15.4" in (fast["gap"] or ""), fast["gap"]
    assert "24.72" not in (fast["gap"] or ""), "绝不能出现未换算得出的荒谬差值"


def test_incomparable_unit_is_not_compared(db_session: Session, monkeypatch):
    """单位不可换算时只列示、不比较（宁可不比，也不误读）。"""
    from app.comparison import analysis as module

    a_id, b_id = _seed_pair(db_session, second_fast_charge="小时")
    # 制造一个无法换算的单位
    monkeypatch.setitem(module._UNIT_CONVERSIONS, "fast_charge", {"分钟": 1.0})
    result = analyze_comparison(db_session, [a_id, b_id])
    fast = next(d for d in result["dimensions"] if d["key"] == "fast_charge")
    assert fast["significant"] is False
    assert fast["gap"] is None
    assert "无法换算" in (fast["note"] or "")


def test_missing_dimension_reported_as_gap(db_session: Session):
    a_id, b_id = _seed_pair(db_session)
    # 删掉 B 的整备质量 → 应进入缺口而不是被推测
    db_session.query(SpecFact).filter(SpecFact.variant_id == b_id,
                                     SpecFact.fact_key == "整备质量(kg)").delete()
    db_session.commit()
    result = analyze_comparison(db_session, [a_id, b_id])
    assert any(g["dimension"] == "整备质量" for g in result["gaps"])
    mass = next(d for d in result["dimensions"] if d["key"] == "mass")
    assert any(v["display"] == "官方资料未披露" for v in mass["values"])


def test_analysis_numbers_whitelist(db_session: Session):
    """答案数字白名单：所有参与比较的数值（含差价）都应登记，供 LLM 答案校验。"""
    a_id, b_id = _seed_pair(db_session)
    result = analyze_comparison(db_session, [a_id, b_id])
    allowed = set(result["allowed_numbers"])
    assert 200.0 in allowed and 150.0 in allowed   # 功率
    assert 20000.0 in allowed                      # 差价
    assert 2.0 in allowed                          # 差价（万元）


def test_render_text_is_deterministic(db_session: Session):
    a_id, b_id = _seed_pair(db_session)
    text = render_analysis_text(analyze_comparison(db_session, [a_id, b_id]))
    assert "取舍" in text and "万元" in text


def test_needs_two_variants(db_session: Session):
    a_id, _ = _seed_pair(db_session)
    assert "error" in analyze_comparison(db_session, [a_id])
