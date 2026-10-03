"""L1 金标语料的**自洽性**测试——不发任何网络请求，CI 可跑。

真正跑模型的抽取质量评测是 `tools/eval_soft_prefs.py`（需要 DEEPSEEK_API_KEY），
**不进 CI**：理由与本仓一贯纪律一致——「不写一条自己跑不通的代码路径让 CI 去
首验」。CI 只验金标文件本身是否合法：

- 字段值全在封闭枚举内（改了 `_USAGE_HINTS` 之类就会红）；
- 空期望样本够多（判据 1/2 靠它们，样本太少等于没判据）；
- 每条都有 note（金标必须写明这条在测什么，否则后人不敢改）。

## 为什么金标入库、而 RAG 题库不入库

`backend/eval/` 整个目录被 gitignore，RAG 题库由脚本从当时的库抽样生成——
所以那套指标只能在同一台机器、同一份题库上比较（见提案 §5.3）。
本文件是手工标注的**封闭集合**，必须进版本控制：金标一改评测结论就变，
而 git 里看得见这个改动。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.eval_soft_prefs import GOLD, check_corpus, load_gold  # noqa: E402


def test_gold_corpus_is_self_consistent():
    """金标文件自洽：枚举合法、id 唯一、note 齐全、空样本够多。"""
    problems = check_corpus(load_gold())
    assert not problems, "金标文件不自洽：\n  - " + "\n  - ".join(problems)


def test_gold_corpus_is_version_controlled():
    """金标必须在 git 里——不在库里，「改了什么导致结论变化」就无从审计。

    ⚠️ 路径要先转成 POSIX 形式再比较：Windows 上是反斜杠，直接
    `endswith("tests/…")` 恒假——这类断言第一次写必踩。
    """
    rel = GOLD.resolve().as_posix()
    assert rel.endswith("backend/tests/soft_prefs_golden.jsonl"), f"金标路径意外：{rel}"
    assert "/eval/" not in rel, "金标不能放进被 gitignore 的 backend/eval/"


def test_gold_corpus_covers_every_soft_pref_field():
    """四个字段都要有正例——否则某个字段的召回率根本没被测到。"""
    cases = load_gold()
    for field in ("usage_scenario", "household_size", "pain_points", "priority_order"):
        hits = [c for c in cases if (c.get("expect") or {}).get(field)]
        assert hits, f"字段 {field} 没有任何正例，召回率无从测量"


def test_gold_corpus_has_adversarial_cases():
    """判据 1/2 依赖对抗样本：空偏好、越界、硬约束不得被抽。"""
    cases = load_gold()
    empty = [c for c in cases if not (c.get("expect") or {})]
    assert len(empty) >= 5, f"空期望样本仅 {len(empty)} 条，判据 1/2 会失去意义"
    ids = {c["id"] for c in cases}
    assert {"e01", "x01", "x02", "h01", "h02", "s01"} <= ids, "对抗样本 id 缺失"


@pytest.mark.parametrize("case_id", ["s01"])
def test_safety_emphasis_gold_stays_empty(case_id):
    """「我最看重安全」必须期望为空——8 维公式里没有任何一个读安全类 fact_key。

    若将来有人给安全加了维度，这条会红，提示金标该同步更新（而不是默默放过）。
    """
    case = next(c for c in load_gold() if c["id"] == case_id)
    assert case["expect"] == {}, f"{case_id} 的期望应当是空：{case['expect']}"
