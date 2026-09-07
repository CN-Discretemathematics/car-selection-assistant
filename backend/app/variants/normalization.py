"""SKU 参数归一化。

对比前统一单位、数字精度、布尔值、枚举值和缺失值。
工况（CLTC/NEDC/WLTC）参与相等性判断：不同工况不直接比较。
"""
from __future__ import annotations

import re
from decimal import Decimal

# 末尾单位括号，如「(km)/(kW)/(L/100km)」：值侧已带单位时才从标签剥离
_TRAILING_UNIT_RE = re.compile(r"[（(][^）)]{0,12}[)）]\s*$")

UNIT_ALIASES = {
    "千瓦": "kW",
    "千瓦时": "kWh",
    "度": "kWh",
    "牛米": "N·m",
    "牛·米": "N·m",
    "牛": "N",
    "毫米": "mm",
    "厘米": "cm",
    "米": "m",
    "公里": "km",
    "千米": "km",
    "升": "L",
    "毫升": "mL",
    "小时": "h",
    "分钟": "min",
    "秒": "s",
    "匹": "hp",
    "马力": "hp",
}

_NUMBER_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*([^\d\s][^\s]*)?$")


def normalize_unit(unit: str | None) -> str:
    if not unit:
        return ""
    u = unit.strip()
    return UNIT_ALIASES.get(u, u)


def normalize_value(value: str | int | float | Decimal, unit: str | None = None) -> tuple[str, str]:
    """把展示值归一化为 (规范化数值, 规范化单位)。

    "150 kW" / "150千瓦" / "150 千瓦" → ("150", "kW")
    """
    text = " ".join(str(value).strip().split())
    m = _NUMBER_RE.match(text)
    if m:
        num = Decimal(m.group(1)).normalize()
        num_text = format(num, "f") if num == num.to_integral() else str(num)
        inferred_unit = m.group(2) or ""
        merged_unit = normalize_unit(unit) or normalize_unit(inferred_unit)
        return num_text, merged_unit
    return text, normalize_unit(unit)


def fact_identity(category: str, fact_key: str, value: str, unit: str | None, cycle: str | None) -> tuple[str, str, str, str, str]:
    """参与「隐藏相同参数」判断的归一化身份。"""
    norm_value, norm_unit = normalize_value(value, unit)
    return (category, fact_key, norm_value, norm_unit, (cycle or "").strip().upper())


def display_fact_value(value: str | None, unit: str | None, cycle: str | None) -> str:
    """带单位与工况的展示值；值缺失时返回空串（由调用方替换为缺失文案）。

    值与单位归一化后拼接，避免「150千瓦 千瓦」这类重复单位。
    """
    if not value:
        return ""
    num, norm_unit = normalize_value(value, unit)
    if num and norm_unit:
        parts = [num, norm_unit]
    else:
        parts = [str(value).strip()]
        if unit:
            parts.append(unit.strip())
    if cycle:
        parts.append(f"({cycle})")
    return " ".join(p for p in parts if p)


def fact_display_label(fact_key: str, unit: str | None = None, cycle: str | None = None) -> str:
    """从原始配置键名派生干净的展示标签，供卡片/对比行名使用。

    增程/插混车的两个续航是**不同参数**（fact_key 不同）：「CLTC纯电续航里程(km)」
    与「CLTC综合续航(km)」。若直接把原始键名当行名，会同时出现前导「CLTC…(km)」与
    值侧「…km (CLTC)」，重复且看起来像两条同一项续航。这里仅剥离一次前导工况词与
    末尾单位，把「纯电续航里程 / 综合续航」区分清楚；单位与工况由值侧 display 承载。

    - 「CLTC纯电续航里程(km)」→「纯电续航里程」
    - 「电动机总功率(kW)」→「电动机总功率」（值侧已带 kW）
    - 「最高车速(km/h)」→ 保留「(km/h)」（unit 未解析，值侧不重复，不丢失单位）
    """
    if not fact_key:
        return ""
    label = fact_key.strip()
    if cycle:
        label = re.sub(rf"^\s*{re.escape(cycle)}\s*", "", label, flags=re.IGNORECASE)
    if unit:
        label = _TRAILING_UNIT_RE.sub("", label)
    return label.strip() or fact_key.strip()
