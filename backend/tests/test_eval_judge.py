"""答案层 LLM-as-judge 评测工具测试（纯函数：JSON 解析 / 分层抽样 / 汇总）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import eval_judge  # noqa: E402


def test_parse_judge_json_plain_and_fenced():
    assert eval_judge._parse_judge_json('{"faithful": true, "unsupported_claims": []}') == {
        "faithful": True, "unsupported_claims": [],
    }
    fenced = '```json\n{"faithful": false, "unsupported_claims": ["X"]}\n```'
    assert eval_judge._parse_judge_json(fenced)["faithful"] is False
    assert eval_judge._parse_judge_json("前面有话。{\"complete\": true}") == {"complete": True}
    assert eval_judge._parse_judge_json("不是 JSON") is None
    assert eval_judge._parse_judge_json("") is None


def test_parse_judge_json_rejects_non_dict():
    assert eval_judge._parse_judge_json("[1,2,3]") is None


def _fake_questions() -> list[dict]:
    qs = []
    for i in range(30):
        qs.append({"id": f"q{i:03d}", "bucket": "parameter", "text": f"参数题{i}"})
    for i in range(20):
        qs.append({"id": f"r{i:03d}", "bucket": "recommend", "text": f"推荐题{i}"})
    return qs


def test_stratified_sample_covers_all_buckets():
    sample = eval_judge._stratified_sample(_fake_questions(), 25, __import__("random").Random(1))
    buckets = {q["bucket"] for q in sample}
    assert buckets == {"parameter", "recommend"}
    assert len(sample) == 25
    # 按比例：per = 25//2 = 12，不足部分按原序补齐（首题 parameter）→ 13/12
    got_p = sum(1 for q in sample if q["bucket"] == "parameter")
    assert got_p == 13


def test_stratified_sample_all_when_under_limit():
    qs = _fake_questions()
    assert len(eval_judge._stratified_sample(qs, 0, __import__("random").Random(1))) == len(qs)


def test_summarize_excludes_unjudged():
    rows = [
        {"faithful": True, "complete": True, "refusal_ok": True},
        {"faithful": False, "complete": None},
        {"faithful": None},  # judge 失败的题不计入分子分母
    ]
    s = eval_judge._summarize(rows)
    assert s["questions"] == 3
    assert s["faithful_rate"] == 0.5 and s["faithful_judged"] == 2
    assert s["complete_rate"] == 1.0 and s["complete_judged"] == 1
    assert s["refusal_ok_rate"] == 1.0
    assert s["double_judge_n"] == 0


def test_summarize_double_judge_agreement():
    rows = [
        {"faithful": True, "faithful_second": True},
        {"faithful": True, "faithful_second": False},
    ]
    s = eval_judge._summarize(rows)
    assert s["double_judge_n"] == 2
    assert s["double_judge_agreement"] == 0.5
