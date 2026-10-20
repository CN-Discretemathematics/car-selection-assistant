"""易混品牌词表的**单一归属**回归测试（2026-10-20）。

背景：此前**有两份不一致的易混品牌词表**——

    app/catalog/brands.py::AMBIGUOUS_BRANDS          6 个
    app/agent/routing.py::_AMBIGUOUS_BRAND_NAMES    11 个（= 上者 + 5 个）

同一个知识切成两半，后果是**硬约束路径与语境路径对同一个词给出不同判定**：
「北京」既是品牌也是日常词（「北京堵车」），在语境判定里被当日常词挡掉，
在硬约束判定里却不被挡。

收拢后：词表由 `app.catalog.brands.BRAND_AMBIGUITY` 唯一持有，
`routing.py` 不再持有本地副本；**各用途的规则仍各自保留**（它们本就不同）。

本文件钉住两件事：

1. **表是一份**——`routing.py` 不得再出现自己的副本（防止再次漂移）；
2. **规则分三套**——同一份表在不同用途下的判定条件不同，且**当前行为不变**
   （收拢是零行为变更的重构）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.catalog.brands import (
    AMBIGUOUS_BRANDS,
    BRAND_AMBIGUITY,
    CONTEXT_ONLY_AMBIGUOUS_BRANDS,
    is_ambiguous_brand,
    resolve_brand_mentions,
)
from tests.seed import make_brand, make_source

ROUTING_PY = Path(__file__).resolve().parents[1] / "app" / "agent" / "routing.py"


# ── 1. 表是一份 ────────────────────────────────────────────────────────────


def test_routing_has_no_local_ambiguous_brand_table():
    """`routing.py` 不得再持有自己的易混品牌词表。

    这条是本次收拢的**核心断言**：两份表就是「知识没有归属」的现场证据。
    只要 routing 里还有一份本地表（或字面量集合），它就会与 brands 那份漂移。

    注意只查**代码**（赋值/集合字面量），不查注释——注释里提到历史名字
    是为了说明「此前是什么样」，那是文档，不是第二份表。
    """
    text = ROUTING_PY.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "_AMBIGUOUS_BRAND_NAMES" not in code, (
        "routing.py 又出现自己的易混品牌词表——词表应统一由 "
        "app.catalog.brands.BRAND_AMBIGUITY 持有"
    )


def test_brand_ambiguity_covers_both_groups():
    """统一表必须**完整覆盖**两组词——漏一个就等于丢了一处保护。"""
    expected = AMBIGUOUS_BRANDS | CONTEXT_ONLY_AMBIGUOUS_BRANDS
    assert set(BRAND_AMBIGUITY) == expected
    assert len(BRAND_AMBIGUITY) == len(expected), "词表里有重复项"


def test_the_two_groups_are_disjoint():
    """两组词不应重叠——重叠会让「哪一组」的区分失去意义。"""
    assert not (AMBIGUOUS_BRANDS & CONTEXT_ONLY_AMBIGUOUS_BRANDS)


# ── 2. 规则分三套（且当前行为不变）────────────────────────────────────────


def test_three_rules_are_independent():
    """同一份表，三种用途的判定条件**不同**——这是设计意图，不是巧合。"""
    # 「大众」：三处都需要保护
    assert is_ambiguous_brand("大众", rule="stem")
    assert is_ambiguous_brand("大众", rule="constraint")
    assert is_ambiguous_brand("大众", rule="context")
    # 「北京」：只在语境上保护（保留 routing 的既有行为）
    assert not is_ambiguous_brand("北京", rule="constraint")
    assert is_ambiguous_brand("北京", rule="context")
    # 非易混品牌：三处都不特殊对待
    for rule in ("stem", "constraint", "context"):
        assert not is_ambiguous_brand("奔驰", rule=rule), rule


def test_unknown_rule_raises():
    """规则名拼错必须**立刻炸**，不能静默按默认走。

    静默 fallback 是这类「一张表三种规则」的结构最危险的失效模式：
    写错规则名 → 落到 else 分支 → 保护失效 → 误报，且没有任何报错。
    """
    with pytest.raises(ValueError, match="未知的歧义判定用途"):
        is_ambiguous_brand("大众", rule="stemm")


# ── 3. 行为等价：收拢不改变既有判定 ───────────────────────────────────────


def _ids(db: Session, message: str) -> set[int]:
    out = resolve_brand_mentions(db, message)
    return set(out.get("brand_ids") or []) if isinstance(out, dict) else set(out)


def test_ambiguous_brand_still_needs_strong_intent_for_constraint(db_session: Session):
    """「理想的预算 20 万」不当作品牌「理想」；「我想买理想」才算。

    这是 `resolve_brand_mentions` 的既有行为，本次收拢**不得改变**它。
    """
    brand = make_brand(db_session, name="理想")
    assert _ids(db_session, "理想的预算 20 万") == set()
    assert _ids(db_session, "我想买理想") == {brand.id}
    assert _ids(db_session, "只要理想") == {brand.id}


def test_context_only_group_keeps_constraint_path_unchanged(db_session: Session):
    """「北京」等第二组词：硬约束路径**不特殊对待**（收拢前的既有行为）。

    收拢把它们并进了统一表，但**没有**给硬约束路径加上强意图词要求——
    那会是行为变更，需生产库验证（本地夹具里这 5 个词从未作为品牌出现）。
    """
    brand = make_brand(db_session, name="北京")
    # 语境路径：日常词用法（无汽车信号）不当品牌
    # 硬约束路径：与收拢前一致——有约束语气即可
    assert _ids(db_session, "只要北京") == {brand.id}


def test_stem_rule_unchanged_for_ambiguous_brand(db_session: Session):
    """易混品牌**不登记简称**（「大众汽车」不该产生「大众」这个简称条目）。"""
    source = make_source(db_session)
    make_brand(db_session, name="大众汽车", source=source)
    from app.catalog.brands import brand_entries

    names = {name for name, _bid, _label in brand_entries(db_session)}
    assert "大众" not in names, "易混品牌不应登记简称"
    assert "大众汽车" in names
