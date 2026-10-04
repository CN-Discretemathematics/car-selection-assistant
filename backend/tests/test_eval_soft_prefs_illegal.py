"""离线评测的越界口径：必须是**真测量**，不是构造性的 0（2026-10-04）。

## 这组测试守的是什么

`tools/eval_soft_prefs.py` 曾对 sanitize **之后**的输出复查「值是否在词表内」，
并把结果当作硬判据。而 `sanitize` → `_pick_value` **已经把词表外的值丢掉了**，
所以那个计数**必然为 0**——工具报「越界枚举值：0（必须为 0）✅ 硬判据通过」，
但它证明不了任何事。

改为消费 `extract_soft_prefs` 的 `illegal_out` 后，口径与线上 `log_shadow` 同源。
本组测试钉住三件事：

1. **越界要能从原始响应里测出来**（不是从清洗结果里复查）；
2. **「没答上来」（`None`）不算越界**——否则会造出纯粹由度量 bug 产生的假警报；
3. **越界率按样本算**（一条样本可能有多条失效，按条目算会 >100%）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from eval_soft_prefs import (  # noqa: E402
    _MAX_EMPTY_RATE,
    _MIN_SAMPLE,
    _OBSERVATIONS,
    score,
    verdict_of,
)

from app.agent import soft_prefs as sp  # noqa: E402


def _item(value: object, evidence: str = "我平时通勤") -> dict:
    return {"value": value, "evidence": evidence}


@pytest.fixture(autouse=True)
def _clear_observations():
    """观测是模块级状态：每个用例前后清空，杜绝跨用例串味。"""
    _OBSERVATIONS.clear()
    yield
    _OBSERVATIONS.clear()


# ── 真正的越界能被测到 ─────────────────────────────────────────────────────
def test_out_of_enum_is_measured():
    illegal: list[dict] = []
    sp.sanitize({"usage_scenario": _item("商务舱")}, "我平时通勤", illegal)

    assert [e["reason"] for e in illegal] == ["out_of_enum"]


def test_bad_type_is_measured():
    illegal: list[dict] = []
    sp.sanitize({"household_size": _item(3)}, "我平时通勤", illegal)

    assert [e["reason"] for e in illegal] == ["bad_type"]


# ── 「没答上来」不是越界（本次实测踩出来的假警报）──────────────────────────
def test_none_value_is_not_illegal():
    """模型对某字段答不出 / 给 null，**不是**「说出了词表外的东西」。

    踩坑实录：这里原先把 `None` 也记成 `bad_type`，金标 25 条实测报出
    「越界率 132%（33 条）」——**33 条全是 `None`**。
    一个由度量 bug 造出来的假警报比假绿更坏：它会让判据永远不通过。
    """
    illegal: list[dict] = []
    out = sp.sanitize(
        {
            "usage_scenario": {"evidence": "我平时通勤"},          # 缺 value 键
            "household_size": {"value": None, "evidence": "我平时通勤"},  # 显式 null
        },
        "我平时通勤",
        illegal,
    )

    assert out == {}
    assert illegal == [], f"「没填」被误记成幻觉：{illegal}"


def test_empty_string_is_not_illegal_either():
    """空串同样是「没答上来」，不是越界。"""
    illegal: list[dict] = []
    sp.sanitize({"usage_scenario": _item("")}, "我平时通勤", illegal)

    assert illegal == [], illegal


# ── score 必须消费观测，而不是复查清洗结果 ────────────────────────────────
def _case(cid: str, text: str, expect: dict | None = None) -> dict:
    return {"id": cid, "text": text, "expect": expect or {}}


def test_score_reads_observations_not_sanitized_output():
    """没有观测 → 记 `unobserved`，**不得**按「干净」计入。

    这正是旧实现的病：sanitize 后永远查不到越界，于是报 0 并判通过。
    """
    cases = [_case("c1", "我最看重动力")]
    results = {"c1": {"pain_points": ["续航"]}}   # 清洗后的结果「看起来很干净」

    rep = score(cases, results)

    assert rep["unobserved"] == 1, rep
    assert rep["responded"] == 0, rep
    assert rep["illegal_rate"] is None


def test_score_counts_observed_violations():
    cases = [_case("c1", "我最看重动力", {"pain_points": ["续航"]})]
    _OBSERVATIONS["c1"] = {
        "status": "ok",
        "illegal": [{"field": "usage_scenario", "reason": "out_of_enum", "value": "商务舱"}],
    }

    rep = score(cases, {"c1": {"pain_points": ["续航"]}})

    assert rep["violated_samples"] == 1, rep
    assert rep["illegal_values"] == 1
    assert rep["illegal_rate"] == 1.0
    assert rep["illegal_by_reason"] == {"out_of_enum": 1}
    assert rep["illegal_by_field"] == {"usage_scenario": 1}
    assert rep["unobserved"] == 0


def test_no_response_excluded_from_rate_denominator():
    """超时不进分母——否则大面积超时会显示成「越界率 0」。"""
    cases = [_case(f"c{i}", "我最看重动力") for i in range(9)]
    cases.append(_case("c9", "我最看重动力", {"pain_points": ["续航"]}))
    for i in range(9):
        _OBSERVATIONS[f"c{i}"] = {"status": "no_response", "illegal": []}
    _OBSERVATIONS["c9"] = {"status": "ok", "illegal": []}

    rep = score(cases, {"c9": {"pain_points": ["续航"]}})

    assert rep["responded"] == 1, rep
    assert rep["illegal_rate"] == 0.0
    assert rep["status_distribution"]["no_response"] == 9


def test_illegal_rate_is_by_sample_not_by_entry():
    """一条样本多条失效时，越界率仍须 ≤ 100%（按条目算会得到 132% 这类荒谬数）。"""
    cases = [_case("c1", "我最看重动力", {"pain_points": ["续航"]})]
    _OBSERVATIONS["c1"] = {
        "status": "ok",
        "illegal": [
            {"field": "usage_scenario", "reason": "out_of_enum", "value": "商务舱"},
            {"field": "household_size", "reason": "bad_type", "value": "3"},
            {"field": "pain_points", "reason": "out_of_enum", "value": "音响改装"},
        ],
    }

    rep = score(cases, {"c1": {"pain_points": ["续航"]}})

    assert rep["violated_samples"] == 1
    assert rep["illegal_values"] == 3
    assert rep["illegal_rate"] == 1.0, "按条目算会得到 3.0（300%）"
    assert 0 <= rep["illegal_rate"] <= 1


def test_unknown_status_does_not_enter_denominator():
    cases = [_case("c1", "我最看重动力", {"pain_points": ["续航"]})]
    _OBSERVATIONS["c1"] = {"status": "unknown", "illegal": []}

    rep = score(cases, {"c1": {"pain_points": ["续航"]}})

    assert rep["responded"] == 0, "漏埋点的记录不得进分母"
    assert rep["status_distribution"]["unknown"] == 1


def test_score_output_is_json_serialisable():
    """--json 要能落盘；新增字段里有 dict/list，序列化不过会直接崩在最后一步。"""
    cases = [_case("c1", "我最看重动力", {"pain_points": ["续航"]})]
    _OBSERVATIONS["c1"] = {
        "status": "ok",
        "illegal": [{"field": "usage_scenario", "reason": "out_of_enum", "value": "商务舱"}],
    }

    rep = score(cases, {"c1": {"pain_points": ["续航"]}})

    json.dumps({k: v for k, v in rep.items() if k != "mismatches"}, ensure_ascii=False)


# ── 判定逻辑（`verdict_of`）：审查实测离线比线上宽松，已对齐 ────────────────
def _rep(n_ok: int, n_all_rejected: int = 0, illegal: int = 0, **over) -> dict:
    """构造一份报告。默认给足 25 条样本、空偏好率 0，只改要测的那一项。"""
    responded = n_ok + n_all_rejected
    base = {
        "empty_rate": 1.0,
        "violated_samples": illegal,
        "illegal_rate": round(illegal / responded, 4) if responded else None,
        "illegal_values": illegal,
        "unobserved": 0,
        "responded": responded,
        "response_empty_rate": round(n_all_rejected / responded, 4) if responded else None,
        "status_distribution": {"ok": n_ok, "all_rejected": n_all_rejected},
        "min_sample": 20,
        "max_empty_rate": 0.5,
    }
    base.update(over)
    return base


def test_clean_sample_passes():
    blockers, ok = verdict_of(_rep(n_ok=25))
    assert ok, blockers


def test_all_rejected_blizzard_is_not_a_pass():
    """25 条全 `all_rejected` + 零越界 → 看着完美，实则模型一次都没按 schema 思考。

    线上 `soft_prefs_report` 早有 `_MAX_EMPTY_RATE` 闸门并在注释里点名这个场景；
    离线原先没有，而本文件的文档字符串却自称「与线上一致」——**不一致**。
    """
    blockers, ok = verdict_of(_rep(n_ok=0, n_all_rejected=25))

    assert not ok
    assert any("空偏好率" in b for b in blockers), blockers


def test_small_sample_is_not_a_pass():
    """3 条响应样本不够支撑切流判据（线上 `_MIN_SAMPLE=20`）。"""
    blockers, ok = verdict_of(_rep(n_ok=3))

    assert not ok
    assert any("样本不足" in b for b in blockers), blockers


def test_unobserved_blocks_pass():
    blockers, ok = verdict_of(_rep(n_ok=25, unobserved=3))

    assert not ok
    assert any("无观测" in b for b in blockers), blockers


def test_no_response_blocks_and_is_reported():
    rep = _rep(n_ok=25)
    rep["status_distribution"] = {"ok": 22, "no_response": 3}
    blockers, ok = verdict_of(rep)

    assert not ok
    assert any("未调通" in b for b in blockers), blockers


def test_fabricated_empty_blocks_pass():
    blockers, ok = verdict_of(_rep(n_ok=25, empty_rate=0.9))

    assert not ok
    assert any("编造" in b for b in blockers), blockers


def test_offline_and_online_gate_constants_match():
    """离线与线上的闸门常量必须一致——不一致会让离线绿灯掩盖线上红灯。"""
    from app.agent import soft_prefs_report

    assert _MIN_SAMPLE == soft_prefs_report._MIN_SAMPLE
    assert _MAX_EMPTY_RATE == soft_prefs_report._MAX_EMPTY_RATE
