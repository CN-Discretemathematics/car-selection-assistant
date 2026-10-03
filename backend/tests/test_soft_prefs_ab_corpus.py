"""L1 接入排序 A/B 语料的**自洽性**测试——不发请求、不连库，CI 可跑。

A/B 本身（`tools/eval_softpref_ab.py`）需要一份装了真实数据的库，**不进 CI**
（理由同本仓纪律：不让 CI 去首验一条自己没跑过的路径）。CI 只校验语料本身：

- `emphasis_dim` 必须是真被测量的 8 维之一（写成别的，L1 根本无权改它）；
- `prefs` 的每个字段都必须在 L1 的封闭 schema 内（越界字段是幻觉，不是偏好）；
- `prefs.priority_order` 与 `emphasis_dim` 必须一致——否则 A/B 测的不是
  「强调了 X 会不会推 X」，而是别的东西；
- 必须留一条**空偏好对照组**（L1 不该动它），否则「有变化」可能全是噪声；
- 语料必须入库（改了它结论就变，git 里得看得见）。

## 为什么「必须留空偏好对照组」

没有对照就无法区分「L1 起了作用」和「这批数据本来就这样」。
`sp06`（我最看重安全 → 无对应维度 → 偏好向量必须为空）就是这条对照：
它必须**完全不变**，否则说明注入逻辑本身有问题。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import soft_prefs as sp  # noqa: E402
from app.agent.tools import DEFAULT_WEIGHTS  # noqa: E402

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "soft_prefs_ab.jsonl"


def load() -> list[dict]:
    return [json.loads(ln) for ln in CORPUS.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_corpus_is_version_controlled_and_out_of_gitignored_dir():
    rel = CORPUS.resolve().as_posix()
    assert rel.endswith("backend/tests/soft_prefs_ab.jsonl"), f"路径意外：{rel}"
    assert "/eval/" not in rel, "A/B 语料不能放进被 gitignore 的 backend/eval/"


def test_emphasis_dim_must_be_a_measured_dimension():
    """emphasis_dim 必须是 8 个**真被测量**的维度之一。

    「安全」不在其中——L1 改不了它，写进语料只会让 A/B 测一个不存在的能力。
    """
    for c in load():
        assert c["emphasis_dim"] in DEFAULT_WEIGHTS, (
            f"{c['id']} 的 emphasis_dim={c['emphasis_dim']!r} 不是真被测量的维度"
        )


def test_prefs_fields_stay_inside_l1_schema():
    """偏好向量的字段不能超出 L1 的封闭 schema。

    越界字段（例如 body_type / energy_type）不是「偏好」而是硬约束——
    它们出现在这里就等于在语料里鼓励 L1 造硬约束。
    """
    allowed = set(sp.FIELDS) if hasattr(sp, "FIELDS") else {
        "usage_scenario", "household_size", "pain_points", "priority_order",
    }
    for c in load():
        for key in (c.get("prefs") or {}):
            assert key in allowed, f"{c['id']} 的 prefs 含越界字段 {key!r}"


def test_priority_order_matches_emphasis_dim():
    """强调维度必须真的是被强调的那个——否则 A/B 测的不是它想测的东西。"""
    for c in load():
        order = (c.get("prefs") or {}).get("priority_order") or []
        if order:
            assert c["emphasis_dim"] in order, (
                f"{c['id']} emphasis_dim={c['emphasis_dim']!r} 不在 priority_order={order} 里"
            )


def test_contains_an_empty_preference_control():
    """必须有一条**空偏好对照组**。

    没有对照，「有变化」分不清是 L1 起的作用还是这批数据本来就这样。
    """
    empties = [c for c in load() if not (c.get("prefs") or {})]
    assert empties, "语料里没有任何空偏好对照组，A/B 结论无法归因"
    for c in empties:
        assert c.get("note"), f"对照组 {c['id']} 必须写明 note（它为什么该不变）"


def test_corpus_covers_multiple_dimensions():
    """强调维度不能只押一个——单维度语料得不出「L1 整体有用」的结论。"""
    dims = {c["emphasis_dim"] for c in load()}
    assert len(dims) >= 3, f"只覆盖了 {dims}，维度太单一"


@pytest.mark.parametrize("field", ["id", "text", "prefs", "emphasis_dim", "budget_cny"])
def test_required_fields_present(field):
    for c in load():
        assert field in c, f"{c.get('id', '?')} 缺字段 {field}"
