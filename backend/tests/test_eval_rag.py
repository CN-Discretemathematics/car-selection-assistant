"""评测口径 v2 单元测试（约束满足度 / 事实覆盖 / 两侧覆盖 / 品牌开放题，纯函数无 DB）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import eval_rag  # noqa: E402
from app.retrieval.backends import SearchResult  # noqa: E402

CTX = {
    "series_attrs": {
        1: {"name": "宋Ultra", "brand_id": 10, "body_type": "suv", "energy_types": {"BEV"},
            "min_price": 100000.0, "max_seats": 5},
        2: {"name": "油耗王", "brand_id": 10, "body_type": "sedan", "energy_types": {"ICE"},
            "min_price": 200000.0, "max_seats": 5},
        3: {"name": "大七座", "brand_id": 11, "body_type": "suv", "energy_types": {"BEV", "PHEV"},
            "min_price": 150000.0, "max_seats": 7},
    },
    "facts": {
        101: {"CLTC纯电续航里程(km)": ("610", "km"), "轴距(mm)": ("2920", "mm")},
        102: {"轴距(mm)": ("2840", "mm")},
    },
    "variants_by_series": {1: [101], 2: [102], 3: [102]},
    "brand_names": {10: "品牌A", 11: "品牌B"},
}


def _hit(series_id: int | None, text: str = "证据") -> SearchResult:
    return SearchResult(chunk_id=f"c{series_id}", score=0.9, text=text, kind="variant_spec", series_id=series_id)


# ── 约束满足度判定 ────────────────────────────────────────────────────────────
def test_series_satisfies_full_constraints():
    constraints = {"budget_max": 150000, "energy_type": "BEV", "body_type": "suv", "passengers": 5}
    assert eval_rag._series_satisfies(CTX["series_attrs"][1], constraints) is True
    # 预算不够 / 能源不符 / 座位不足：任一约束不满足即不相关（宁可少推不可推错）
    assert eval_rag._series_satisfies(CTX["series_attrs"][1], {**constraints, "budget_max": 80000}) is False
    assert eval_rag._series_satisfies(CTX["series_attrs"][1], {**constraints, "energy_type": "ICE"}) is False
    assert eval_rag._series_satisfies(CTX["series_attrs"][1], {**constraints, "passengers": 6}) is False
    assert eval_rag._series_satisfies(CTX["series_attrs"][2], constraints) is False  # 轿车+燃油
    assert eval_rag._series_satisfies(None, constraints) is False


def test_series_satisfies_new_energy():
    assert eval_rag._series_satisfies(CTX["series_attrs"][1], {"new_energy": True}) is True
    assert eval_rag._series_satisfies(CTX["series_attrs"][2], {"new_energy": True}) is False  # 纯燃油


def test_constraint_metrics_counts_valid_only():
    q = {"bucket": "recommend", "expect": {"energy_type": "BEV"}}
    hits = [_hit(1), _hit(2), _hit(1), _hit(2), _hit(2)]  # 2/5 有效
    out = eval_rag._constraint_metrics(q, hits, CTX, 5)
    assert out["valid_hit@5"] == 1.0
    assert out["valid_precision@5"] == 0.4
    assert out["valid_mrr"] == 1.0


def test_constraint_metrics_all_invalid():
    q = {"bucket": "recommend", "expect": {"energy_type": "BEV"}}
    hits = [_hit(2)] * 5
    out = eval_rag._constraint_metrics(q, hits, CTX, 5)
    assert out["valid_hit@5"] == 0.0 and out["valid_mrr"] == 0.0


# ── 事实覆盖（参数题）────────────────────────────────────────────────────────
def test_fact_coverage_hit_and_miss():
    q = {"bucket": "parameter", "expect": {"fact_key": "CLTC纯电续航里程(km)"}, "anchors": {"series_id": 1}}
    good = [_hit(1, "宋Ultra 标准版（纯电，指导价 10 万）核心参数：CLTC纯电续航里程(km) = 610 km。")]
    bad = [_hit(1, "宋Ultra 标准版核心参数：轴距(mm) = 2920 mm。")]
    assert eval_rag._fact_coverage(q, good, CTX, {1: 1}, 5) == 1.0
    assert eval_rag._fact_coverage(q, bad, CTX, {1: 1}, 5) == 0.0


def test_fact_coverage_none_when_fact_absent():
    # 数据里没有该参数：覆盖无从谈起（不可回答题另测），返回 None 不计入指标
    q = {"bucket": "parameter", "expect": {"fact_key": "最大马力(Ps)"}, "anchors": {"series_id": 1}}
    assert eval_rag._fact_coverage(q, [_hit(1)], CTX, {1: 1}, 5) is None


def test_fact_needles_avoid_bare_number():
    needles = eval_rag._fact_needles("轴距(mm)", "2920", "mm")
    assert "轴距(mm) = 2920" in needles and "2920 mm" in needles
    # 值自带单位或无单位时不追加重复 needle
    assert eval_rag._fact_needles("电池能量(kWh)", "105.7kWh", "kWh") == ["电池能量(kWh) = 105.7kWh"]
    assert eval_rag._fact_needles("级别", "中大型SUV", None) == ["级别 = 中大型SUV"]


# ── 判定模式路由 ──────────────────────────────────────────────────────────────
def test_judge_mode_routing():
    assert eval_rag._judge_mode({"bucket": "parameter", "expect": {"fact_key": "轴距(mm)"}}, CTX) == "fact"
    assert eval_rag._judge_mode({"bucket": "compare"}, CTX) == "pair"
    assert eval_rag._judge_mode({"bucket": "semantic", "text": "帮我推荐一台纯电的SUV，家用"}, CTX) == "constraint"
    # 点名车系的车系问答：单答案意图，沿用 series 级旧指标
    named = {"bucket": "recommend", "text": "宋Ultra 的纯电版有哪些款型",
             "anchors": {"series_id": 1}, "expect": {"energy_type": "BEV", "series_id": 1}}
    assert eval_rag._judge_mode(named, CTX) is None
    unnamed = {"bucket": "recommend", "text": "预算15万，想要一台纯电SUV，5口人",
               "anchors": {"series_id": 1}, "expect": {"budget_max": 150000, "energy_type": "BEV",
                                                        "body_type": "suv", "passengers": 5}}
    assert eval_rag._judge_mode(unnamed, CTX) == "constraint"
    brand = {"bucket": "recommend", "text": "品牌A 有哪些在售的新能源SUV", "anchors": {},
             "expect": {"brand_name": "品牌A"}}
    assert eval_rag._judge_mode(brand, CTX) == "brand"


# ── 对比题两侧覆盖 / 品牌开放题 ───────────────────────────────────────────────
def test_pair_coverage_requires_both_series():
    q = {"bucket": "compare", "anchors": {"variant_ids": [101, 102]}, "expect": {"variant_ids": [101, 102]}}
    both = [_hit(1), _hit(3)]
    one = [_hit(1), _hit(1)]
    assert eval_rag._pair_coverage(q, both, {101: 1, 102: 3}, 5) == 1.0
    assert eval_rag._pair_coverage(q, one, {101: 1, 102: 3}, 5) == 0.0
    # 同车系款型对比：series 级 Hit 已覆盖，pair 指标不适用
    assert eval_rag._pair_coverage({"anchors": {"variant_ids": [101, 101]}}, both, {101: 1}, 5) is None


def test_brand_hit():
    q = {"bucket": "recommend", "expect": {"brand_name": "品牌A"}}
    assert eval_rag._brand_hit(q, [_hit(1)], CTX, 5) == 1.0
    assert eval_rag._brand_hit(q, [_hit(3)], CTX, 5) == 0.0
