"""参数归一化单元测试。"""
from __future__ import annotations

from app.variants.normalization import display_fact_value, fact_display_label, fact_identity, normalize_value


def test_normalize_kw_aliases():
    assert normalize_value("150 kW") == ("150", "kW")
    assert normalize_value("150千瓦", "千瓦") == ("150", "kW")
    assert normalize_value("150 千瓦", "kW") == ("150", "kW")


def test_normalize_decimal_precision():
    assert normalize_value("12.500", "L") == ("12.5", "L")
    assert normalize_value("18", "kW") == ("18", "kW")


def test_fact_identity_ignores_display_noise_but_keeps_cycle():
    a = fact_identity("电池和续航", "range_km", "500", "km", "CLTC")
    b = fact_identity("电池和续航", "range_km", "500公里", "公里", "CLTC")
    c = fact_identity("电池和续航", "range_km", "500", "km", "NEDC")
    assert a == b
    assert a != c


def test_display_dedupes_unit_and_marks_missing():
    # 值与单位重复时去重：150千瓦 + 千瓦 → "150 kW"
    assert display_fact_value("150千瓦", "千瓦", None) == "150 kW"
    assert display_fact_value("150", "kW", None) == "150 kW"
    assert display_fact_value("500", "km", "CLTC") == "500 km (CLTC)"
    assert display_fact_value("多连杆式独立悬架", None, None) == "多连杆式独立悬架"
    # 缺失值由调用方替换为「官方资料未披露」
    assert display_fact_value(None, "km", None) == ""
    assert display_fact_value("", "km", None) == ""


def test_fact_display_label_distinguishes_dual_cltc_range():
    # 增程/插混车的两个续航为不同参数：剥离前导工况词与末尾单位，值侧仍带 km (CLTC)
    assert fact_display_label("CLTC纯电续航里程(km)", "km", "CLTC") == "纯电续航里程"
    assert fact_display_label("CLTC综合续航(km)", "km", "CLTC") == "综合续航"
    assert fact_display_label("NEDC纯电续航里程(km)", "km", "NEDC") == "纯电续航里程"
    # 单位已由值侧承载时剥离，避免「电动机总功率(kW) / 150 kW」重复
    assert fact_display_label("电动机总功率(kW)", "kW", None) == "电动机总功率"
    # unit 未解析（值侧不重复单位）：保留末尾单位，不丢失信息
    assert fact_display_label("最高车速(km/h)", None, None) == "最高车速(km/h)"
    # 无单位/无工况的普通键名原样保留
    assert fact_display_label("多连杆式独立悬架", None, None) == "多连杆式独立悬架"
    assert fact_display_label("", None, None) == ""
