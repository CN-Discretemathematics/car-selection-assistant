"""`series_coverage` 的口径测试。

## 为什么要单独测

1. **口径必须与既有工具一致**：`tools/export_series_gaps.py::coverage()` 是
   运维看缺口的口径来源。新的看板指标若与它打架，运维看到的和看板看到的
   就不一样——那比没有看板更糟。
2. **「真缺口」与「全部停售」必须分开**：
   「有款型但全部停售」是**上游标记的真实状态**，详情页显示「暂无在售款型数据」
   本就正确，**不是回补目标**。混在一起会让回补指标永不收敛
   （2026-10-20 复核 D1.2 时纠正的旧口径误判）。
3. **不能触发索引重建**：覆盖率会随回补单调下降，若并进 `_counts`
   （索引重建的触发条件），回补一批款型就会重建全量索引。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.rag.service import _counts, db_counts, series_coverage
from tests.seed import (
    make_brand,
    make_series,
    make_source,
    make_variant,
    make_year,
)


def _make_off_sale_variant(db: Session, series, source, config: str = "停售版"):
    """造一个**停售**款型（status != on_sale）。"""
    year = make_year(db, series)
    v = make_variant(db, series, year, config_version=config, source=source)
    v.status = "off_sale"
    db.flush()
    return v


# ── 1. 口径 ────────────────────────────────────────────────────────────────


def test_coverage_counts_distinguish_three_states(db_session: Session):
    """三种车系状态各一个，数字必须分得清。

    - 甲：有在售款型        → 计入 series_on_sale
    - 乙：只有停售款型      → 计入 gap_only_off_sale，**不是**真缺口
    - 丙：完全没款型        → 计入 gap_no_variants（真缺口）
    """
    source = make_source(db_session)
    brand = make_brand(db_session, name="覆盖测试厂", source=source)

    s_a = make_series(db_session, brand, name="甲有在售", source=source)
    y = make_year(db_session, s_a)
    make_variant(db_session, s_a, y, config_version="在售版", source=source)

    s_b = make_series(db_session, brand, name="乙全部停售", source=source)
    _make_off_sale_variant(db_session, s_b, source)

    s_c = make_series(db_session, brand, name="丙无款型", source=source)
    assert s_c.id  # 无款型车系：建出来但不给它款型
    db_session.commit()

    cov = series_coverage(db_session)
    assert cov["series_active"] == 3
    assert cov["series_on_sale"] == 1
    assert cov["gap_no_variants"] == 1, "完全没款型才是真缺口"
    assert cov["gap_only_off_sale"] == 1, "有款型但全停售不是真缺口"
    assert cov["series_with_variants"] == 2


def test_off_sale_only_series_is_not_counted_as_gap(db_session: Session):
    """**全部停售的车系不进真缺口**——这是 D1.2 纠正的核心。

    旧口径（方案原文）把「31 个全部款型停售的车系」列为待回补对象，
    而 `export_series_gaps.py` 的 docstring 早就写明那是真实状态。
    """
    source = make_source(db_session)
    brand = make_brand(db_session, name="停售测试厂", source=source)
    s = make_series(db_session, brand, name="全停售车系", source=source)
    _make_off_sale_variant(db_session, s, source)
    db_session.commit()

    cov = series_coverage(db_session)
    assert cov["gap_no_variants"] == 0, "有款型（哪怕全停售）不算真缺口"
    assert cov["gap_only_off_sale"] == 1


def test_inactive_series_excluded(db_session: Session):
    """非在售车系（车系或品牌非 active）不计入任何口径。"""
    source = make_source(db_session)
    brand = make_brand(db_session, name="非在售厂", source=source)
    s = make_series(db_session, brand, name="停售车系", source=source)
    s.active_status = "archived"
    db_session.commit()

    cov = series_coverage(db_session)
    assert cov["series_active"] == 0


# ── 2. 与既有工具口径一致 ──────────────────────────────────────────────────


def test_coverage_parity_with_export_series_gaps(db_session: Session):
    """与 `tools/export_series_gaps.py::coverage()` 逐项一致。

    两处数字若打架，运维看到的和看板看到的就会不一样。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import export_series_gaps as gaps

    source = make_source(db_session)
    brand = make_brand(db_session, name="口径测试厂", source=source)
    s1 = make_series(db_session, brand, name="有一款", source=source)
    y = make_year(db_session, s1)
    make_variant(db_session, s1, y, config_version="在售版", source=source)
    s2 = make_series(db_session, brand, name="全停售", source=source)
    _make_off_sale_variant(db_session, s2, source)
    s3 = make_series(db_session, brand, name="无款型", source=source)
    assert s3.id  # 无款型车系
    db_session.commit()

    new = series_coverage(db_session)
    old = gaps.coverage(db_session)

    assert new["series_active"] == old["series_total"]
    assert new["gap_no_variants"] == old["gap_no_variants"]
    assert new["gap_only_off_sale"] == old["gap_only_off_sale"]
    assert new["series_on_sale"] == old["covered_series"]


# ── 3. 恒等式 ──────────────────────────────────────────────────────────────


def test_coverage_internal_consistency(db_session: Session):
    """恒等式：有款型 = 在售 - 真缺口；有在售 = 有款型 - 全部停售。"""
    source = make_source(db_session)
    brand = make_brand(db_session, name="恒等厂", source=source)
    s = make_series(db_session, brand, name="某车系", source=source)
    y = make_year(db_session, s)
    make_variant(db_session, s, y, config_version="版", source=source)
    db_session.commit()

    cov = series_coverage(db_session)
    assert cov["series_with_variants"] == cov["series_active"] - cov["gap_no_variants"]
    assert cov["series_on_sale"] == cov["series_with_variants"] - cov["gap_only_off_sale"]


# ── 4. 不得触发索引重建 ────────────────────────────────────────────────────


def test_coverage_not_in_db_counts(db_session: Session):
    """覆盖率**不得**进 `db_counts`——那是索引重建的触发条件。

    `_counts` 用于判断「库内规模变了没 → 要不要重建索引」。
    覆盖率会随回补单调下降，若并进去，回补一批款型就会重建一次全量索引，
    为了一个给人看的数字付出重建代价。
    """
    counts = db_counts(db_session)
    assert "series_active" not in counts
    assert "gap_no_variants" not in counts
    assert "gap_only_off_sale" not in counts
    assert "series_on_sale" not in counts
    # 而 _counts 的四元组长度不变——它仍是索引重建的判据
    assert len(_counts(db_session)) == 4
