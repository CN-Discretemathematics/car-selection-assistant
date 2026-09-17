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


def test_dual_motor_subkeys_are_excluded(db_session: Session):
    """第三轮复审 BLOCKED：动力/扭矩的子串匹配会命中「前/后电动机功率」分项键。

    双电机款型必须取**整车**最大功率；若另一款型只有分电机键，则该维度只列示不比较。
    """
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    s1 = make_series(db_session, brand, name="双电机车系", energy_types=("BEV",), source=source)
    s2 = make_series(db_session, brand, name="单电机车系", energy_types=("BEV",), source=source)
    y1 = make_year(db_session, s1)
    y2 = make_year(db_session, s2)
    dual = make_variant(db_session, s1, y1, config_version="四驱", energy_type="BEV",
                        price_cny="300000", source=source)
    solo = make_variant(db_session, s2, y2, config_version="后驱", energy_type="BEV",
                        price_cny="200000", source=source)
    # 双电机款型：整车功率 + 前后分项（分项绝不能被取为整车值）
    for key, value in (("最大功率(kW)", "400"), ("前电动机最大功率(kW)", "180"),
                       ("后电动机最大功率(kW)", "220")):
        db_session.add(SpecFact(variant_id=dual.id, category="参数信息", fact_key=key,
                                fact_value=value, unit="kW", source_id=source.id))
    # 单电机款型：只有分电机键（整车键缺失）→ 该维度对它不可比
    db_session.add(SpecFact(variant_id=solo.id, category="参数信息", fact_key="后电动机最大功率(kW)",
                            fact_value="150", unit="kW", source_id=source.id))
    db_session.commit()

    result = analyze_comparison(db_session, [dual.id, solo.id])
    power = next(d for d in result["dimensions"] if d["key"] == "power")
    by_variant = {v["variant_id"]: v for v in power["values"]}
    assert by_variant[dual.id]["display"] == "400 kW", "必须取整车功率而不是分电机功率"
    assert by_variant[solo.id]["display"] == "官方资料未披露"
    assert power["significant"] is False and power["gap"] is None, "可比数值不足 → 只列示"


def test_cross_cycle_not_compared(db_session: Session):
    """第三轮复审 BLOCKED：CLTC 与 WLTC 工况不同 → 只列示并标注，不输出差值。

    违背对比页脚注「不同工况的续航与油耗不直接比较」与 §7.3 工况口径纪律。
    """
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    s1 = make_series(db_session, brand, name="甲车系", energy_types=("BEV",), source=source)
    s2 = make_series(db_session, brand, name="乙车系", energy_types=("BEV",), source=source)
    y1 = make_year(db_session, s1)
    y2 = make_year(db_session, s2)
    v1 = make_variant(db_session, s1, y1, config_version="CLTC版", energy_type="BEV",
                      price_cny="200000", source=source)
    v2 = make_variant(db_session, s2, y2, config_version="WLTC版", energy_type="BEV",
                      price_cny="200000", source=source)
    db_session.add(SpecFact(variant_id=v1.id, category="参数信息", fact_key="CLTC纯电续航里程(km)",
                    fact_value="310", unit="km", cycle="CLTC", source_id=source.id))
    db_session.add(SpecFact(variant_id=v2.id, category="参数信息", fact_key="WLTC纯电续航里程(km)",
                    fact_value="500", unit="km", cycle="WLTC", source_id=source.id))
    db_session.commit()

    result = analyze_comparison(db_session, [v1.id, v2.id])
    range_dim = next(d for d in result["dimensions"] if d["key"] == "range")
    assert range_dim["significant"] is False, "跨工况不得比较"
    assert "工况不同" in (range_dim["note"] or "")
    assert range_dim["gap"] is None


def test_fast_charge_converted_leader_gap_uses_canonical_unit(db_session: Session):
    """第三轮复审 BLOCKED：换算款型成为领先者时，gap 文案必须用规范单位（分钟）。

    A 写「0.68 小时」（=40.8 分钟，慢）、B 写「0.42 小时」（=25.2 分钟，快）——
    B 领先且其 fact 的原单位是「小时」，gap 文案曾错误输出「低 13.4 小时」。
    """
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    s1 = make_series(db_session, brand, name="慢充车系", energy_types=("BEV",), source=source)
    s2 = make_series(db_session, brand, name="快充车系", energy_types=("BEV",), source=source)
    y1 = make_year(db_session, s1)
    y2 = make_year(db_session, s2)
    v1 = make_variant(db_session, s1, y1, config_version="慢充", energy_type="BEV",
                      price_cny="200000", source=source)
    v2 = make_variant(db_session, s2, y2, config_version="快充", energy_type="BEV",
                      price_cny="200000", source=source)
    db_session.add(SpecFact(variant_id=v1.id, category="参数信息", fact_key="电池快充时间(小时)",
                    fact_value="0.68", unit=None, source_id=source.id))
    db_session.add(SpecFact(variant_id=v2.id, category="参数信息", fact_key="电池快充时间(小时)",
                    fact_value="0.42", unit=None, source_id=source.id))
    db_session.commit()

    result = analyze_comparison(db_session, [v1.id, v2.id])
    fast = next(d for d in result["dimensions"] if d["key"] == "fast_charge")
    assert "小时" not in (fast["gap"] or ""), f"gap 必须用规范单位（分钟）：{fast['gap']}"
    assert "分钟" in (fast["gap"] or "")


def test_values_carry_source_id(db_session: Session):
    """第三轮复审 Major：分析结果必须带 source_id，否则 Agent 引用永远为空。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    series = make_series(db_session, brand, name="甲车系", energy_types=("ICE",), source=source)
    year = make_year(db_session, series)
    v1 = make_variant(db_session, series, year, config_version="1.5T", energy_type="ICE",
                      price_cny="150000", source=source)
    v2 = make_variant(db_session, series, year, config_version="2.0T", energy_type="ICE",
                      price_cny="180000", source=source)
    for vid, power in ((v1.id, "130"), (v2.id, "162")):
        db_session.add(SpecFact(variant_id=vid, category="参数信息", fact_key="最大功率(kW)",
                                fact_value=power, unit="kW", source_id=source.id))
    db_session.commit()

    result = analyze_comparison(db_session, [v1.id, v2.id])
    power_dim = next(d for d in result["dimensions"] if d["key"] == "power")
    assert all(v.get("source_id") == source.id for v in power_dim["values"])


def test_render_text_is_deterministic(db_session: Session):
    a_id, b_id = _seed_pair(db_session)
    text = render_analysis_text(analyze_comparison(db_session, [a_id, b_id]))
    assert "取舍" in text and "万元" in text


def test_needs_two_variants(db_session: Session):
    a_id, _ = _seed_pair(db_session)
    assert "error" in analyze_comparison(db_session, [a_id])


def test_analysis_has_verdict_and_key_points(db_session: Session):
    """面板「先结论后细节」：verdict 一句话 + 关键差异 Top3（按差距百分比从大到小）。"""
    import re

    a_id, b_id = _seed_pair(db_session)
    result = analyze_comparison(db_session, [a_id, b_id])

    verdict = result["verdict"] or ""
    assert "甲车系" in verdict and "乙车系" in verdict, verdict
    assert "指导价最低" in verdict
    # verdict 是纯模板（只插值款型 label 与 leaders），断言其确定性组成：
    # 以「总体：」开头，且每个款型的领先维度都来自 leaders 字段（不允许出现库外事实）
    assert verdict.startswith("总体：")
    a_row = next(v for v in result["variants"] if v["variant_id"] == a_id)
    assert a_row["label"] in verdict
    if a_row["leaders"]:
        assert "、".join(a_row["leaders"][:2]) in verdict

    points = result["key_points"]
    assert 1 <= len(points) <= 3
    pcts = [float(re.search(r"差距 ([0-9.]+)%", p["gap"]).group(1)) for p in points]
    assert pcts == sorted(pcts, reverse=True), f"Top3 应按差距从大到小：{pcts}"
    sig_labels = {d["label"] for d in result["dimensions"] if d["significant"]}
    variant_labels = {v["label"] for v in result["variants"]}
    for p in points:
        assert p["winner"] in variant_labels
        assert p["label"] in sig_labels, f"key_point 必须来自显著维度：{p}"


def test_render_text_leads_with_verdict(db_session: Session):
    a_id, b_id = _seed_pair(db_session)
    result = analyze_comparison(db_session, [a_id, b_id])
    text = render_analysis_text(result)
    assert text.startswith(result["verdict"])


def test_conflicting_duplicate_keys_are_flagged_not_compared(db_session: Session):
    """真实事故回归（2026-09-16 卡罗拉锐放）：库内同名参数有两个不同取值时，
    该维度必须标注「存疑」且**不参与比较**——绝不能任选一行造出假差异。

    事故：混动车的 `最大功率(kW)` 在源页面出现两次（系统综合 144 / 发动机净功率 116），
    两款实际同为 144kW，却因两行在不同款型里覆盖顺序不同，得出「144 vs 116、差 24%」。
    """
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="丰田", source=source)

    def build(name: str, price: str) -> int:
        series = make_series(db_session, brand, name=name, energy_types=("HEV",), source=source)
        year = make_year(db_session, series)
        variant = make_variant(db_session, series, year, config_version="先锋版",
                               energy_type="HEV", price_cny=price, source=source)
        # 同名键两行、值不同（顺序刻意在两款型间相反，复现事故的数据形态）
        return variant.id

    a_id = build("卡罗拉锐放A", "130000")
    b_id = build("卡罗拉锐放B", "140000")
    pairs = {a_id: [("144", "kW"), ("116", "kW")], b_id: [("116", "kW"), ("144", "kW")]}
    for vid, rows in pairs.items():
        for value, unit in rows:
            db_session.add(SpecFact(variant_id=vid, category="参数信息", fact_key="最大功率(kW)",
                                    fact_value=value, unit=unit, source_id=source.id))
        # 系统综合功率与净功率各自有明确键（去重时的依据）
        db_session.add(SpecFact(variant_id=vid, category="参数信息", fact_key="系统综合功率(kW)",
                                fact_value="144", unit="kW", source_id=source.id))
    db_session.commit()

    result = analyze_comparison(db_session, [a_id, b_id])
    power = next(d for d in result["dimensions"] if d["key"] == "power")
    assert power["significant"] is False, "存疑维度不得判为显著差异"
    assert power["gap"] is None
    assert "存疑" in (power["note"] or ""), power["note"]
    assert all("存疑" in v["display"] for v in power["values"]), power["values"]
    # 不得进入 Top3、不得进入 verdict 的领先项
    assert all(p["label"] != "动力（最大功率）" for p in result["key_points"])
    verdict = result["verdict"] or ""
    assert "动力" not in verdict, verdict
