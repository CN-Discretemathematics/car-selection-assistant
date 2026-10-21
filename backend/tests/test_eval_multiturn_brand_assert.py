"""品牌级排除断言（`brand_not_recommended`）的回归测试。

## 为什么要有这条

计划 §七的第一条判断是「**硬约束排除项**是记忆的第一价值」，
而断言类型此前**只有车系级**（`not_recommended`）⇒ 品牌级排除**根本没有断言可用**
⇒ 那类场景连「测」都测不了（2026-10-21 补）。

## 空转防护

断言查的是**结构化**推荐结果里的 `brand_name`，
**不是**正文裸字符串——解释里可能写「也可以看看 XX」而它**并没有被推荐**。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.eval_multiturn import AssertionFailed, check_assertion


def _out(brands: list[str], series: str = "某车系") -> SimpleNamespace:
    return SimpleNamespace(
        recommended_variants=[
            SimpleNamespace(series_name=series, brand_name=b) for b in brands
        ],
        explanation="",
    )


def test_brand_absent_passes() -> None:
    """推荐里没有该品牌 ⇒ 断言成立 ✓。"""
    check_assertion(_out(["丰田", "本田"]), {"type": "brand_not_recommended", "brand": "比亚迪"})


def test_brand_present_fails() -> None:
    """推荐里有该品牌 ⇒ 断言不成立，必须红 ✓。"""
    with pytest.raises(AssertionFailed):
        check_assertion(_out(["丰田", "比亚迪"]), {"type": "brand_not_recommended", "brand": "比亚迪"})


def test_prose_mention_does_not_count() -> None:
    """正文提到该品牌、但**没被推荐** ⇒ 断言仍成立（只认结构化字段）✓。"""
    out = SimpleNamespace(
        recommended_variants=[],
        explanation="也可以看看比亚迪的秦",
    )
    check_assertion(out, {"type": "brand_not_recommended", "brand": "比亚迪"})


def test_missing_brand_field_is_rejected() -> None:
    """缺 `brand` 字段 ⇒ 直接判失败（不能默默放过）✓。"""
    with pytest.raises(AssertionFailed):
        check_assertion(_out([]), {"type": "brand_not_recommended"})


def test_brand_name_prefix_still_hits() -> None:
    """品牌名是前缀/包含关系时也要命中（库内品牌名与用户说法可能略有出入）✓。"""
    with pytest.raises(AssertionFailed):
        check_assertion(
            _out(["北京汽车制造厂"]),
            {"type": "brand_not_recommended", "brand": "北京汽车"},
        )
