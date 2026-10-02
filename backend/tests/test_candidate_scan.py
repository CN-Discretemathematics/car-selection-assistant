"""候选集规模的**可观测性**（2026-10-02 P5.1 / H2）。

`recommendation_tool` 的硬约束 stmt **故意不加 LIMIT**——排名由 8 维软评分决定，
评分需要全量事实，SQL 侧复现不了同一排序；随手加 limit 会让「取前 N 条」退化成
「随便取 N 条」，推荐结果静默改变且无法证明被丢掉的更差。

代价是真的：用户没给预算/品牌/锁定时匹配**全库**在售款型。本轮**不改行为**，
只把量测出来——一次 COUNT(*) 比物化全表便宜得多，「多大」从此是可观测事实。

真要收敛时，本测试就是回归底线：**行为不能变**。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.schemas import UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import (
    make_brand,
    make_sales,
    make_series,
    make_source,
    make_variant,
    make_year,
)


def _seed(db: Session, n: int, *, locked_series: int | None = None) -> None:
    src = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试", source=src)
    for i in range(n):
        s = make_series(db, brand, name=f"车系{i}", body_type="sedan",
                        energy_types=("BEV",), source=src)
        y = make_year(db, s)
        make_variant(db, s, y, config_version="标准版", energy_type="BEV",
                     price_cny=str(100000 + i * 1000), source=src)
        make_sales(db, s, "2026-08", 1000, source=src)


def test_candidates_scanned_is_reported(db_session: Session):
    """返回体必须带上候选集规模，否则「多大」永远只是猜测。"""
    _seed(db_session, 4)
    out = recommendation_tool(db_session, UserProfile(), limit=5)
    assert "candidates_scanned" in out
    assert out["candidates_scanned"] >= out["count"] > 0


def test_budget_filter_shrinks_candidates(db_session: Session):
    """预算等硬约束缩小候选集——这正是「先过滤再评分」的意义。"""
    _seed(db_session, 6)
    loose = recommendation_tool(db_session, UserProfile(), limit=10)
    tight = recommendation_tool(
        db_session, UserProfile(budget={"min": 100000, "max": 101500}), limit=10
    )
    assert tight["candidates_scanned"] < loose["candidates_scanned"]
    assert tight["count"] <= 2


def test_limit_does_not_change_which_variants_win(db_session: Session):
    """核心回归：limit 只截断输出，**不改变排名与入选者**。

    这条是「不加 SQL LIMIT」的全部理由：若日后有人改成 SQL 侧 limit，
    这条会立刻失败——被截掉的行可能正是分数最高的那几个。
    """
    _seed(db_session, 12)
    wide = recommendation_tool(db_session, UserProfile(), limit=100)
    narrow = recommendation_tool(db_session, UserProfile(), limit=3)

    assert [v["variant_id"] for v in narrow["variants"]] == [
        v["variant_id"] for v in wide["variants"][:3]
    ]
    assert narrow["count"] == wide["count"], "评分条数不受 limit 影响"
    assert narrow["candidates_scanned"] == wide["candidates_scanned"]


@pytest.mark.parametrize("limit", [1, 3, 5, 50])
def test_output_never_exceeds_limit(db_session: Session, limit: int):
    _seed(db_session, 8)
    out = recommendation_tool(db_session, UserProfile(), limit=limit)
    assert len(out["variants"]) <= limit
