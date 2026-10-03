"""来源引用块的统一实现与其既有差异（2026-10-02 抽取）。

「查 Source → 循环 → Citation」这段此前在 4 处逐字复制。抽取时逐处对照，
发现**循环上界并不一致**：

  | 调用点                     | 循环上界                | label 后缀 |
  |----------------------------|-------------------------|------------|
  | _comparison_analysis_reply | source_ids[:3]          | 配置数据   |
  | _catalog_overview_reply    | **source_ids（无上限）** | 车型数据   |
  | _brand_overview_reply      | source_ids[:3]          | 车型数据   |
  | _tool_loop_reply           | sorted(source_ids)[:3]  | 数据       |

盘点回答至今不下限、其余三条限 3 —— 这是**既有行为**，不是本轮引入的。
本轮只统一机制、把差异用参数显式暴露，不擅自改口径（是否该统一属产品决策）。
本测试的作用是**钉住这一现状**：将来若有人「顺手统一」，测试会先失败并说明理由。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.engine import CITATION_LIMIT, _source_citations
from tests.seed import make_source


def _seed_sources(db: Session, n: int) -> list[int]:
    ids = []
    for i in range(n):
        ids.append(make_source(db, name=f"来源{i + 1}").id)
    db.commit()
    return ids


def test_empty_input_returns_no_citations(db_session: Session):
    assert _source_citations(db_session, [], label_suffix="配置数据") == []


def test_default_limit_is_three(db_session: Session):
    """默认上界 = CITATION_LIMIT = 3（与三条历史实现一致）。"""
    assert CITATION_LIMIT == 3
    ids = _seed_sources(db_session, 5)
    out = _source_citations(db_session, ids, label_suffix="配置数据")
    assert len(out) == 3
    assert [c.source_id for c in out] == ids[:3]


def test_limit_none_emits_all(db_session: Session):
    """limit=None → 全量（盘点回答的既有行为）。"""
    ids = _seed_sources(db_session, 5)
    out = _source_citations(db_session, ids, label_suffix="车型数据", limit=None)
    assert len(out) == 5


def test_label_uses_source_name_with_fallback(db_session: Session):
    ids = _seed_sources(db_session, 1)
    out = _source_citations(db_session, ids, label_suffix="配置数据")
    assert out[0].label == "来源1 配置数据"
    assert out[0].source_name == "来源1"


def test_unknown_source_id_falls_back_to_generic_label(db_session: Session):
    """查不到来源名时兜底为「来源」，不编造。"""
    out = _source_citations(db_session, [999999], label_suffix="数据")
    assert out[0].label == "来源 数据"
    assert out[0].source_name is None


def test_explicit_sort_is_caller_responsibility(db_session: Session):
    """排序由调用方决定（_tool_loop_reply 传 sorted(...)）——本函数不重排。"""
    ids = _seed_sources(db_session, 3)
    shuffled = list(reversed(ids))
    out = _source_citations(db_session, shuffled, label_suffix="数据")
    assert [c.source_id for c in out] == shuffled, "函数不得擅自重排，否则会改动既有顺序"


@pytest.mark.parametrize("suffix", ["配置数据", "车型数据", "数据"])
def test_suffix_is_the_only_differing_input(db_session: Session, suffix: str):
    """后缀是唯一需要按调用点区分的输入——这正是统一后的接口形状。"""
    ids = _seed_sources(db_session, 2)
    out = _source_citations(db_session, ids, label_suffix=suffix)
    assert all(c.label.endswith(suffix) for c in out)
