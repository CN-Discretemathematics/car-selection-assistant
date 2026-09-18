# -*- coding: utf-8 -*-
"""意图路由金标集评测（P1）：逐条跑 `decide_route`（不调 LLM），断言 intent 匹配。

金标集：`backend/tests/routing_golden.jsonl`（与消费它的测试同目录；`backend/eval/`
整体被 .gitignore 忽略、不会入库，故不放那里），问法全部来自仓库内既有回归测试
（test_catalog_count / test_tool_loop / test_tool_loop_gate / test_tool_loop_scope /
test_brand_constraint / test_series_qa / test_agent）与已知边界样本；
每行 `{"utterance", "expect_intent", "known_gap", "note"}`。

- `known_gap=false` 的行是**当前路由已支持**的问法：断言 intent 必须精确匹配；
- `known_gap=true` 的行是**当前路由已知不支持**的多轮上下文/复合句样本：
  入集记录差距、跳过断言（不计失败），报告里同时打印当前实际落点供演进参考。

覆盖率阈值：`BASELINE_COVERAGE` 写死于下方常量（= 本实现完成时的实测值），
未来任何路由改动导致覆盖率跌破基线即为回归。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.agent.engine import extract_hints, merge_profile
from app.agent.routing import INTENTS, asks_brand_lineup, asks_catalog_count, decide_route
from app.agent.schemas import UserProfile
from app.catalog.brands import resolve_brand_mentions
from app.catalog.series_index import resolve_series
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

GOLDEN_PATH = Path(__file__).resolve().parent / "routing_golden.jsonl"
# 基线（实现完成实测）：断言行 83/83 全部命中，覆盖率 1.0；known_gap 8 行跳过断言。
# 防回退语义：新增金标行若被正确路由则保持 1.0；任何路由回归使覆盖率跌破基线即失败。
BASELINE_COVERAGE = 1.0


def _seed(db: Session) -> None:
    """标准种子：5 品牌 / 6 在售车系（覆盖品牌盘点、车系问答、能源构成）。"""
    source = make_source(db, name="汽车之家")
    toyota = make_brand(db, name="丰田", source=source)
    byd = make_brand(db, name="比亚迪", source=source)
    geely = make_brand(db, name="银河", source=source)
    benz = make_brand(db, name="奔驰", source=source)
    leapmotor = make_brand(db, name="零跑", source=source)
    for brand, name, body, energy, price in (
        (toyota, "凯美瑞", "sedan", "ICE", "200000"),
        (toyota, "汉兰达", "suv", "HEV", "280000"),
        (byd, "汉L", "suv", "PHEV", "259800"),
        (geely, "星愿", "sedan", "BEV", "74000"),
        (benz, "奔驰A级", "sedan", "ICE", "260000"),
        (leapmotor, "零跑A10", "suv", "BEV", "89000"),
    ):
        series = make_series(db, brand, name=name, body_type=body, energy_types=(energy,), source=source)
        year = make_year(db, series)
        make_variant(db, series, year, config_version="标准版", energy_type=energy,
                     price_cny=price, source=source)
    db.commit()


def _route(db: Session, message: str):
    """复刻 engine.handle() 的有状态序章（不含会话存储与锁定车系），再取路由决策。

    与 handle() 一致：extract_hints → merge_profile → resolve_series →
    resolve_brand_mentions(assume_constraint=盘点/计数问法) → 品牌线索并入画像
    并置 structured → decide_route。known_gap 行涉及的**多轮**上下文不在此复刻，
    由跳过断言覆盖。
    """
    hints = extract_hints(message)
    profile = merge_profile(UserProfile(), hints)
    structured = bool(hints)
    resolved = resolve_series(db, message)
    brand_hints = resolve_brand_mentions(
        db, message,
        series_names=[s.name for s, _brand in resolved],
        assume_constraint=asks_brand_lineup(message) or asks_catalog_count(message),
    )
    if brand_hints:
        profile = merge_profile(profile, brand_hints)
        structured = True
    return decide_route(message, hints, structured, profile, resolved, db)


def _load_golden() -> list[dict]:
    rows: list[dict] = []
    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


GOLDEN = _load_golden()


def test_golden_set_wellformed():
    """金标集本体质量门：≥60 条、字段齐全、intent 合法、问法不重复。"""
    assert len(GOLDEN) >= 60, f"金标集应≥60 条，实际 {len(GOLDEN)}"
    for row in GOLDEN:
        assert row["expect_intent"] in INTENTS, row
        assert row["utterance"].strip(), row
        assert isinstance(row.get("known_gap"), bool), row
        assert row.get("note"), row
    utterances = [row["utterance"] for row in GOLDEN]
    assert len(utterances) == len(set(utterances)), "金标集问法不得重复"


def test_golden_set_covers_all_intents():
    """金标集必须覆盖全部 8 个 intent（known_gap 行不计入）。"""
    asserted_intents = {row["expect_intent"] for row in GOLDEN if not row.get("known_gap")}
    assert asserted_intents == set(INTENTS), f"缺少覆盖的 intent：{set(INTENTS) - asserted_intents}"


@pytest.mark.parametrize("row", GOLDEN, ids=lambda row: row["utterance"][:28])
def test_routing_golden(db_session: Session, row: dict):
    """逐条断言：known_gap 行跳过，其余 intent 必须精确匹配。"""
    _seed(db_session)
    if row.get("known_gap"):
        pytest.skip(f"已知差距（仅入集记录，不计失败）：{row.get('note', '')}")
    decision = _route(db_session, row["utterance"])
    assert decision.intent == row["expect_intent"], (
        f"路由漂移：{row['utterance']!r} 期望 {row['expect_intent']}，"
        f"实际 {decision.intent}（rule={decision.matched_rule}）"
    )


def test_routing_golden_coverage_report(db_session: Session):
    """覆盖率报告 + 阈值断言（防路由回退）。报告用 -s 可见。"""
    _seed(db_session)
    total = len(GOLDEN)
    known_gap = [row for row in GOLDEN if row.get("known_gap")]
    asserted = [row for row in GOLDEN if not row.get("known_gap")]
    matched: list[str] = []
    misses: list[str] = []
    gap_actual: list[str] = []
    for row in asserted:
        decision = _route(db_session, row["utterance"])
        if decision.intent == row["expect_intent"]:
            matched.append(row["utterance"])
        else:
            misses.append(
                f"{row['utterance']!r}: 期望 {row['expect_intent']}，实际 {decision.intent}"
            )
    for row in known_gap:
        decision = _route(db_session, row["utterance"])
        gap_actual.append(f"{row['utterance']!r}: 当前={decision.intent} 期望={row['expect_intent']}")
    coverage = (len(matched) / len(asserted)) if asserted else 0.0

    report = (
        "\n===== 意图路由金标集覆盖率报告 =====\n"
        f"金标集总数：{total}（断言 {len(asserted)} + known_gap 跳过 {len(known_gap)}）\n"
        f"断言命中：{len(matched)}/{len(asserted)}\n"
        f"覆盖率：{coverage:.2%}（阈值 ≥ {BASELINE_COVERAGE:.2%}）\n"
        f"intent 覆盖：{sorted({r['expect_intent'] for r in asserted})}\n"
        "known_gap 行当前实际落点（仅记录，不计失败）：\n  " + "\n  ".join(gap_actual) +
        "\n===================================="
    )
    print(report)
    assert not misses, f"路由回归 {len(misses)} 条：\n" + "\n".join(misses)
    assert coverage >= BASELINE_COVERAGE, (
        f"覆盖率 {coverage:.2%} 跌破基线 {BASELINE_COVERAGE:.2%}（路由回退）"
    )
