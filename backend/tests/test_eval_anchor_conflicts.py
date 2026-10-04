"""语料锚点自洽性审计（`_anchor_text_conflicts`）的测试。

## 为什么要有这个检查

`_portable_anchors` 的判据 1 是「id 在本库存在**且其名称出现在问句里**→ 信任 id」。
但「2024款 2.0T 四驱旗舰版」这种**通用款型名跨车系重名**，于是属于别的车系的
id 也「看起来对得上」，被判据 1 原样信任——**它发现不了这类错位**。

实测命中（2026-10-04）：「英菲尼迪QX60 的 2024款 2.0T 四驱旗舰版 和 …」
问句明写 QX60，锚点却指向「红旗HQ9 PHEV」。

这类题同时污染两个方向：指标侧压低 `pair_coverage`，修复侧让以锚点为准的
补召回机制插入错误车系的切片。

**本检查只报告、不改判据**：改锚点口径会让全部历史基线失效，那是人的决定。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.eval_rag import _anchor_text_conflicts


class _FakeDB:
    """只提供 `db.scalars(select(VehicleSeries))` 这一个用法。"""

    def __init__(self, series):
        self._series = series

    def scalars(self, *_a, **_k):
        return iter(self._series)


def _s(sid: int, name: str):
    return SimpleNamespace(id=sid, name=name)


@pytest.fixture
def variant_series() -> dict[int, int]:
    # 变体 id -> 车系 id
    # 900/901 同属「红旗HQ9 PHEV」(10)：模拟通用款型名跨车系重名导致的锚点错位
    return {900: 10, 901: 10, 902: 20, 903: 30}


def _q(text: str, *, series_id=None, variant_ids=None, qid="q1"):
    anchors: dict = {}
    if series_id is not None:
        anchors["series_id"] = series_id
    if variant_ids is not None:
        anchors["variant_ids"] = variant_ids
    return {"id": qid, "text": text, "anchors": anchors}


def test_detects_question_contradicting_its_anchor(variant_series):
    """问句明写「英菲尼迪QX60」但两侧锚点都指向红旗HQ9 → 必须被抓出来。

    这就是实测到的形态：`_portable_anchors` 的判据 1 按**通用款型名**
    （`2024款 2.0T 四驱旗舰版`）匹配上了属于红旗的那个变体 id，于是原样信任。
    """
    db = _FakeDB([_s(10, "红旗HQ9 PHEV"), _s(20, "英菲尼迪QX60")])
    questions = [
        _q("英菲尼迪QX60 的 2024款 2.0T 四驱旗舰版 和 2024款 2.0T 四驱卓越版 差别在哪",
           variant_ids=[900, 901], qid="q0007")
    ]

    conflicts = _anchor_text_conflicts(db, questions, variant_series)

    assert len(conflicts) == 1, conflicts
    assert conflicts[0]["anchor_series"] == [10]      # 锚点两侧都指向红旗
    assert conflicts[0]["text_series"] == [20]        # 问句说的是 QX60


def test_no_conflict_when_anchor_matches_text(variant_series):
    """锚点与问句一致 → 不报（否则就是噪声，没人看了）。"""
    db = _FakeDB([_s(10, "红旗HQ9 PHEV"), _s(20, "英菲尼迪QX60")])
    questions = [
        _q("英菲尼迪QX60 的 2024款 2.0T 四驱旗舰版 和 2024款 四驱卓越版 差别在哪",
           variant_ids=[902], qid="q0007")
    ]

    assert _anchor_text_conflicts(db, questions, variant_series) == []


def test_no_conflict_when_question_names_no_series(variant_series):
    """问句里没有任何本库车系名 → 无从矛盾，不报。"""
    db = _FakeDB([_s(10, "红旗HQ9 PHEV"), _s(20, "英菲尼迪QX60")])
    questions = [_q("帮我对比 2026款 280TSI 手动先锋版 和 2025款 都市版 的配置差异",
                    variant_ids=[900], qid="q0001")]

    assert _anchor_text_conflicts(db, questions, variant_series) == []


def test_detects_partially_misaligned_anchor(variant_series):
    """**部分**错位也必须抓出来——这是实测到的真实形态。

    语料里这类题同时带 `series_id` 与 `variant_ids`：`_portable_anchors` 的
    规则 2 会按问句里的车系名把 `series_id` **正确**重解析到 QX60，
    但 `variant_ids` 因「通用款型名重名」仍指向红旗。
    于是两个集合**有交集但不相等**——第一版判据用「有交集就放过」，
    结果本地 520 题实测报出 0 条，把 3 条真实错位全漏了。
    """
    db = _FakeDB([_s(10, "红旗HQ9 PHEV"), _s(20, "英菲尼迪QX60")])
    questions = [_q(
        "英菲尼迪QX60 的 2024款 2.0T 四驱旗舰版 和 2024款 2.0T 四驱卓越版 差别在哪",
        series_id=20, variant_ids=[900, 901], qid="q0007",
    )]

    conflicts = _anchor_text_conflicts(db, questions, variant_series)

    assert len(conflicts) == 1, conflicts
    assert conflicts[0]["partial"] is True
    assert conflicts[0]["anchor_series"] == [10, 20]   # 红旗残留 + QX60（已重解析）
    assert conflicts[0]["text_series"] == [20]


def test_no_conflict_when_question_mentions_anchor_series_among_many(variant_series):
    """问句里出现了多个车系名，但**包含**锚点那个 → 不算矛盾。"""
    db = _FakeDB([_s(10, "红旗HQ9 PHEV"), _s(20, "英菲尼迪QX60"), _s(30, "坦克500新能源")])
    questions = [
        _q("红旗HQ9 PHEV 和 坦克500新能源 的差别在哪", variant_ids=[900, 903], qid="q0009")
    ]

    assert _anchor_text_conflicts(db, questions, variant_series) == []


def test_question_without_anchor_is_skipped(variant_series):
    """没有锚点的题（按 brand/fact 判定的）不参与这个检查。"""
    db = _FakeDB([_s(10, "红旗HQ9 PHEV")])
    questions = [_q("红旗HQ9 PHEV 怎么样", variant_ids=None)]

    assert _anchor_text_conflicts(db, questions, variant_series) == []


def test_unknown_variant_id_is_ignored_not_crashed(variant_series):
    """锚点里出现本库不存在的变体 id → 跳过它，别抛异常。"""
    db = _FakeDB([_s(10, "红旗HQ9 PHEV"), _s(20, "英菲尼迪QX60")])
    questions = [_q("英菲尼迪QX60 差别在哪", variant_ids=[999999])]

    assert _anchor_text_conflicts(db, questions, variant_series) == []
