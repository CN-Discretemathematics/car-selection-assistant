"""名称解析的单字车系名与「短名出现在后面」两个缺陷的回归测试。

**缺陷 1**：`series_index._load_name_entries` 的 `len(norm) >= 2` 本是为砍
「领克20」误伤「20万」这类**纯数字**短名，却把**单汉字**车系名一起砍了。
实测全库只有 3 个单字车系名（比亚迪汉 20 款 / 比亚迪夏 4 款 / 长城炮 62 款），
它们**全部**进不了索引——「汉怎么样」「汉的续航多少」「炮怎么样」一律解析出 0 个。

**缺陷 2**：重叠判定用 `msg.find(norm)`，**只看首次出现**。短名出现在**后面**时
会被误杀：「汉L和汉怎么选」里「汉」在位置 4，但 find 返回 0（落在「汉l」的跨度内），
于是用户点名的「汉」被静默丢掉。

2026-10-05 用户在两档方案中选「只放开汉和炮」（夏挡住——「夏天买车合适吗」
实测会误判为比亚迪夏）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.catalog.series_index import (
    _SINGLE_CHAR_SERIES_ALLOWED,
    resolve_series,
)
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> None:
    source = make_source(db, name="解析测试来源")
    brands: dict[str, object] = {}
    for series_name, brand_name, prices in (
        ("汉", "比亚迪", [160000, 180000]),
        ("汉L", "比亚迪", [210000, 240000]),
        ("夏", "比亚迪", [150000]),
        ("炮", "长城", [120000, 130000]),
        ("汉兰达", "丰田", [280000, 300000]),
        ("金刚炮", "长城", [140000, 150000]),
    ):
        # 品牌名有唯一约束，同一品牌只能建一次
        if brand_name not in brands:
            brands[brand_name] = make_brand(db, name=brand_name, source=source)
        brand = brands[brand_name]
        series = make_series(db, brand, name=series_name, source=source)
        year = make_year(db, series)
        for index, price in enumerate(prices):
            make_variant(
                db, series, year, config_version=f"款{index}", price_cny=price,
                source=source,
            )
    db.commit()


def _names(db: Session, message: str) -> set[str]:
    return {series.name for series, _brand in resolve_series(db, message)}


# ── 缺陷 1：单字车系名 ───────────────────────────────────────────────────
def test_allowed_single_char_series_resolve(db_session: Session):
    _seed(db_session)
    assert _names(db_session, "汉怎么样") == {"汉"}
    assert _names(db_session, "炮怎么样") == {"炮"}
    assert "汉" in _SINGLE_CHAR_SERIES_ALLOWED and "炮" in _SINGLE_CHAR_SERIES_ALLOWED


def test_blocked_single_char_series_does_not_resolve(db_session: Session):
    """「夏」不在允许表里——用户拍板挡住，因���「夏天买车合适吗」会误判。"""
    _seed(db_session)
    assert "夏" not in _SINGLE_CHAR_SERIES_ALLOWED
    assert _names(db_session, "夏怎么样") == set()
    assert _names(db_session, "夏天买车合适吗") == set(), "「夏天」不得被当成比亚迪夏"


def test_numeric_short_names_still_blocked(db_session: Session):
    """`len>=2` 当初要挡的是**纯数字**短名（领克20 vs 20万），那道闸不能被放松。"""
    _seed(db_session)
    assert _names(db_session, "预算20万要家用SUV") == set()


# ── 缺陷 2：短名出现在后面 ───────────────────────────────────────────────
def test_short_name_before_long_name(db_session: Session):
    _seed(db_session)
    assert _names(db_session, "汉和汉L怎么选") == {"汉", "汉L"}


def test_short_name_after_long_name(db_session: Session):
    """**关键用例**：短名在长名**之后**。旧实现 `msg.find` 只看首次出现，
    「汉L和汉怎么选」里「汉」的位置 4 被 find 误报成 0（落在「汉l」跨度内）→ 被跳过。"""
    _seed(db_session)
    assert _names(db_session, "汉L和汉怎么选") == {"汉", "汉L"}
    assert _names(db_session, "汉兰达和汉哪个好") == {"汉兰达", "汉"}


def test_long_name_still_wins_when_short_is_its_prefix(db_session: Session):
    """单字被包含在长名里时**不该**拆出幽灵实体（否则会把单车系误判成对比）。"""
    _seed(db_session)
    assert _names(db_session, "汉兰达怎么样") == {"汉兰达"}
    assert _names(db_session, "金刚炮怎么样") == {"金刚炮"}


def test_three_way_comparison_all_resolved(db_session: Session):
    _seed(db_session)
    assert _names(db_session, "汉 汉L 秦PLUS 三个怎么选") >= {"汉", "汉L"}
