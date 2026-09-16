# -*- coding: utf-8 -*-
"""存量修复工具与解析层去重规则的**交叉一致性**测试。

工具（backend/tools/dedupe_conflicting_facts.py）故意内联了去重规则——它要能对
「修复前」的部署执行（那时 app 里还没有 dedupe_duplicate_keys），因此这里断言
两处实现在同一批样例上给出完全一致的结论：规则漂移会立刻变红。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_BACKEND))

from app.sources.autohome_sku import dedupe_duplicate_keys as parser_dedupe  # noqa: E402
from tools.dedupe_conflicting_facts import dedupe_duplicate_keys as tool_dedupe  # noqa: E402


def _fact(key: str, value: str, unit: str | None = None, fid: int | None = None) -> dict:
    item = {"fact_key": key, "value": value, "unit": unit}
    if fid is not None:
        item["id"] = fid
    return item


# 真实事故形态 + 若干边界：同键同值、同键冲突但值有专键承载、冲突值无处承载
FIXTURES = [
    [_fact("最大功率(kW)", "126", "kW"), _fact("最大功率(kW)", "126", "kW")],
    [_fact("最大功率(kW)", "144", "kW"), _fact("最大功率(kW)", "116", "kW"),
     _fact("系统综合功率(kW)", "144", "kW"), _fact("最大净功率(kW)", "116", "kW")],
    [_fact("最大功率(kW)", "116", "kW"), _fact("最大功率(kW)", "144", "kW"),
     _fact("系统综合功率(kW)", "144", "kW"), _fact("最大净功率(kW)", "116", "kW")],
    [_fact("最大功率(kW)", "100", "kW"), _fact("最大功率(kW)", "110", "kW")],
    [_fact("最大扭矩(N·m)", "188", None), _fact("最大扭矩(N·m)", "205", None),
     _fact("最大扭矩(N·m)", "188", None)],
    # 同义不同粒度（车身结构）：保留信息量最大的那个
    [_fact("车身结构", "两厢车"), _fact("车身结构", "5门5座两厢车")],
    [_fact("车身结构", "5门5座两厢车"), _fact("车身结构", "两厢车")],
    # 非包含关系（保留全部，交分析层标存疑）
    [_fact("车身结构", "两厢车"), _fact("车身结构", "三厢车")],
]


def test_tool_and_parser_rules_agree():
    for i, facts in enumerate(FIXTURES):
        tool_out = tool_dedupe([dict(f) for f in facts])
        parser_out = parser_dedupe([dict(f) for f in facts])
        key = lambda items: sorted((f["fact_key"], f["value"]) for f in items)  # noqa: E731
        assert key(tool_out) == key(parser_out), f"样例 {i} 两处规则不一致：{tool_out} vs {parser_out}"


def test_rules_are_conservative_on_uncovered_conflicts():
    """冲突值无处承载时必须保留全部（不丢数据），两处实现都要如此。"""
    facts = [_fact("最大功率(kW)", "100", "kW"), _fact("最大功率(kW)", "110", "kW")]
    assert len(tool_dedupe(facts)) == 2
    assert len(parser_dedupe(facts)) == 2


def test_dedupe_keeps_most_specific_for_substring_values():
    """同义不同粒度：保留信息量最大的值（车身结构 5门5座两厢车 ⊃ 两厢车）。"""
    out = tool_dedupe([_fact("车身结构", "两厢车"), _fact("车身结构", "5门5座两厢车")])
    assert len(out) == 1 and out[0]["value"] == "5门5座两厢车"

    # 非包含关系不适用该规则 → 保留全部（交分析层标存疑）
    kept = tool_dedupe([_fact("车身结构", "两厢车"), _fact("车身结构", "三厢车")])
    assert len(kept) == 2
    assert len(parser_dedupe([_fact("车身结构", "两厢车"), _fact("车身结构", "三厢车")])) == 2
