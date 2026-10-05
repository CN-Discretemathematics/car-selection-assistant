"""对比链路按**实际车系数**说话（2026-10-05 端到端复验发现）。

`resolve_series` 的 docstring 写明「**最多 4 个**」，而对比渲染通篇假设恰好两个：

    blocks = ["你说的这两款车我先放在一起看："]     # 写死
    diff.append(f"{label}：{values[0]} vs {values[1]}")   # values[2:] 被丢掉
    first, second = resolved[0][0], resolved[1][0]         # 小结只比前两台

用户问「汉、汉L、秦PLUS 怎么选」的真实输出（改前）：卡片里三个车系块都在，
**对比行里只有「秦PLUS vs 汉」，汉L 整个消失**，小结还写「两款车定位不同」——
用户完全看不出第三款没被比。这与本项目反复修的「截断了但没说截断」是同一类。
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.agent.series_qa import _count_phrase, build_series_qa_answer
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

F_SIZE = "长*宽*高(mm)"

FACTS = {
    "A": [("尺寸", "长*宽*高(mm)", "4800*1900*1500", "mm", None),
          ("动力", "电动机总功率(kW)", "200", "kW", None)],
    "B": [("尺寸", "长*宽*高(mm)", "4900*1950*1520", "mm", None),
          ("动力", "电动机总功率(kW)", "250", "kW", None)],
    "C": [("尺寸", "长*宽*高(mm)", "5000*1970*1540", "mm", None),
          ("动力", "电动机总功率(kW)", "310", "kW", None)],
    "D": [("尺寸", "长*宽*高(mm)", "5100*1990*1560", "mm", None),
          ("动力", "电动机总功率(kW)", "380", "kW", None)],
}


def _seed(db: Session, name: str, tag: str, price: str, positioning: str = "中大型车"):
    brand = make_brand(db, f"多车品牌{tag}")
    series = make_series(db, brand, name)
    series.positioning = positioning
    year = make_year(db, series)
    source = make_source(db)
    make_variant(
        db, series, year, config_version="标准版", price_cny=price,
        facts=FACTS[tag], source=source,
    )
    return series, brand


def _text(db: Session, names, prices, positioning="中大型车") -> str:
    resolved = [
        _seed(db, name, "ABCD"[index], price, positioning)
        for index, (name, price) in enumerate(zip(names, prices, strict=True))
    ]
    return build_series_qa_answer(db, resolved, "、".join(names) + " 怎么选")


def _diff_line(text: str) -> str:
    return next(
        line for line in text.splitlines() if line.startswith("同量纲参数对比")
    )


# ── 数量词 ───────────────────────────────────────────────────────────────
def test_count_phrase():
    assert _count_phrase(2) == "这两款车"
    assert _count_phrase(3) == "这三款车"
    assert _count_phrase(4) == "这四款车"
    assert _count_phrase(5) == "这 5 款车"


# ── 三车系：不得静默丢车 ─────────────────────────────────────────────────
def test_three_series_all_appear_in_comparison(db_session: Session):
    """核心回归：改前 `values[0] vs values[1]`，第三台在对比行里**完全消失**。"""
    text = _text(
        db_session, ["甲车", "乙车", "丙车"], ["200000", "250000", "300000"]
    )
    size_seg = next(
        seg for seg in _diff_line(text).split("；") if "尺寸：" in seg
    )
    for value in ("4800*1900*1500", "4900*1950*1520", "5000*1970*1540"):
        assert value in size_seg, f"{value} 没出现在对比行：{size_seg}"
    assert size_seg.count(" vs ") == 2, size_seg  # 三个值 = 两个 vs


def test_three_series_header_and_summary_count_them(db_session: Session):
    """开头与小结都不得再说「两款车」——用户问了三台。"""
    text = _text(
        db_session, ["甲车", "乙车", "丙车"], ["200000", "250000", "300000"]
    )
    assert "这三款车" in text, text[:200]
    assert "两款车" not in text, "仍在说「两款车」"
    assert "小结：这几款车" in text


def test_three_series_summary_names_every_series(db_session: Session):
    """定位不一致时，小结必须**逐个点名**，不能只说头两台。"""
    db = db_session
    a, ba = _seed(db, "甲车", "A", "200000", "紧凑型车")
    b, bb = _seed(db, "乙车", "B", "250000", "中大型车")
    c, bc = _seed(db, "丙车", "C", "300000", "中大型SUV")
    text = build_series_qa_answer(db, [(a, ba), (b, bb), (c, bc)], "甲车乙车丙车怎么选")
    summary = next(line for line in text.splitlines() if line.startswith("小结"))
    for name in ("甲车", "乙车", "丙车"):
        assert name in summary, f"{name} 没被点名：{summary}"


def test_four_series_all_appear(db_session: Session):
    """`resolve_series` 上限就是 4，四个也必须一个不少。"""
    text = _text(
        db_session, ["甲车", "乙车", "丙车", "丁车"],
        ["200000", "250000", "300000", "350000"],
    )
    size_seg = next(
        seg for seg in _diff_line(text).split("；") if "尺寸：" in seg
    )
    assert size_seg.count(" vs ") == 3, size_seg
    assert "5100*1990*1560" in size_seg, size_seg
    assert "这四款车" in text


# ── 两车系：行为不得变 ───────────────────────────────────────────────────
def test_two_series_unchanged(db_session: Session):
    """两车系走原来的成对口径——本轮不得回归已修好的 N2 价格判定。

    甲车 20 万、乙车 25 万：两个单点区间**不重叠**，所以措辞是「没有重叠」。
    """
    text = _text(db_session, ["甲车", "乙车"], ["200000", "250000"])
    assert "这两款车" in text
    assert "小结：两款车同属「中大型车」级别、价格区间没有重叠" in text, text
    size_seg = next(
        seg for seg in _diff_line(text).split("；") if "尺寸：" in seg
    )
    assert size_seg.count(" vs ") == 1, size_seg


def _seed_range(db: Session, name: str, tag: str, low: str, high: str):
    """造一个**有价格区间**的车系（单个在售款是点价格，区间重叠判定对它没意义）。"""
    brand = make_brand(db, f"区间品牌{tag}")
    series = make_series(db, brand, name)
    series.positioning = "中大型车"
    year = make_year(db, series)
    source = make_source(db)
    for i, price in enumerate((low, high)):
        make_variant(
            db, series, year, config_version=f"款{i}", price_cny=price,
            facts=FACTS[tag], source=source,
        )
    return series, brand


def test_two_series_overlap_still_detected(db_session: Session):
    """N2 的原口径：区间真重叠时必须说「高度重叠」（不是点价格，是真区间）。"""
    a, ba = _seed_range(db_session, "甲车", "A", "200000", "280000")
    b, bb = _seed_range(db_session, "乙车", "B", "260000", "340000")
    text = build_series_qa_answer(db_session, [(a, ba), (b, bb)], "甲车乙车怎么选")
    assert "价格区间高度重叠" in text, text


def test_two_series_no_overlap_still_detected(db_session: Session):
    a, ba = _seed_range(db_session, "甲车", "A", "100000", "150000")
    b, bb = _seed_range(db_session, "乙车", "B", "200000", "250000")
    text = build_series_qa_answer(db_session, [(a, ba), (b, bb)], "甲车乙车怎么选")
    assert "价格区间没有重叠" in text, text


def test_two_series_different_class_unchanged(db_session: Session):
    a, ba = _seed(db_session, "甲车", "A", "200000", "紧凑型车")
    b, bb = _seed(db_session, "乙车", "B", "250000", "中大型车")
    text = build_series_qa_answer(db_session, [(a, ba), (b, bb)], "甲车乙车怎么选")
    assert "小结：两款车定位不同（「紧凑型车」vs「中大型车」）" in text, text


# ── 价格口径（N≥3）──────────────────────────────────────────────────────
def test_three_series_reports_span_and_overlap(db_session: Session):
    """N≥3 改说**整体跨度 + 有无任意一对重叠**。"""
    text = _text(
        db_session, ["甲车", "乙车", "丙车"], ["100000", "200000", "300000"]
    )
    summary = next(line for line in text.splitlines() if line.startswith("小结"))
    assert "指导价跨度 10.00-30.00 万" in summary, summary
    assert "价格区间互不重叠" in summary, summary


def test_three_series_reports_overlap_when_present(db_session: Session):
    """三台里只要**有一对**区间重叠就必须说出来（单点价格互相不碰的情形不算）。"""
    db = db_session
    a, ba = _seed_range(db, "甲车", "A", "100000", "150000")
    b, bb = _seed_range(db, "乙车", "B", "200000", "300000")
    c, bc = _seed_range(db, "丙车", "C", "280000", "340000")
    text = build_series_qa_answer(db, [(a, ba), (b, bb), (c, bc)], "甲车乙车丙车怎么选")
    summary = next(line for line in text.splitlines() if line.startswith("小结"))
    assert "其中有价格区间重叠" in summary, summary
    assert "指导价跨度 10.00-34.00 万" in summary, summary


def test_three_series_says_nothing_when_price_missing(db_session: Session):
    """任一车系价格未披露 → **什么都不说**（N2 的口径），不拿已知的两台编结论。"""
    from app.common.models import OfficialPrice, VehicleVariant

    db = db_session
    a, ba = _seed(db, "甲车", "A", "200000")
    b, bb = _seed(db, "乙车", "B", "250000")
    c, bc = _seed(db, "丙车", "C", "260000")
    # 删掉丙车的官方指导价，制造「任一方未披露」
    variant_ids = [
        v.id for v in db.query(VehicleVariant).filter_by(series_id=c.id).all()
    ]
    db.query(OfficialPrice).filter(OfficialPrice.variant_id.in_(variant_ids)).delete()
    db.flush()

    text = build_series_qa_answer(db, [(a, ba), (b, bb), (c, bc)], "甲车乙车丙车怎么选")
    summary = next(line for line in text.splitlines() if line.startswith("小结"))
    assert "指导价跨度" not in summary, summary
    assert "重叠" not in summary, summary


def test_summary_never_mentions_两款_for_three(db_session: Session):
    """反向断言：任何三车系回答里都不得出现「两款车」。"""
    text = _text(db_session, ["甲车", "乙车", "丙车"], ["100000", "200000", "300000"])
    assert not re.search(r"两款车", text), text


# ── 审查 P1-3：尺寸口径必须**逐字一致** ──────────────────────────────────
def _seed_size_tier(db: Session, name: str, brand_name: str, sizes: list[str]):
    """造一个尺寸**多档、且众数 ≠ 首值**的车系——正是两种实现会分道扬镳的形状。

    品牌名**不能**包含车系名，否则 `display_name` 会拼成「品牌车系车系」而测试取不到块头。
    """
    brand = make_brand(db, brand_name)
    series = make_series(db, brand, name)
    series.positioning = "中大型车"
    year = make_year(db, series)
    source = make_source(db)
    for index, size in enumerate(sizes):
        make_variant(
            db, series, year, config_version=f"款{index}",
            price_cny=200000 + index * 10000,
            # 首条故意是**非众数**的那个，`rank_headlines` 的 text 模式会取它
            facts=[("尺寸", F_SIZE, size, "mm", None),
                   ("动力", "电动机总功率(kW)", "200", "kW", None)],
            source=source,
        )
    return series, brand


def _size_in_block(text: str, name: str) -> str:
    """取某车系块里「核心参数」那一行的「尺寸」项。

    块头（【X】…）与核心参数行是**两行**，所以要把块头和下一行拼起来再切。
    """
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith(f"【{name}】"))
    joined = lines[idx] + "\n" + lines[idx + 1]
    core = joined.split("核心参数（全系极值）：")[1]
    return next(p for p in core.split("；") if p.startswith("尺寸 ")).removeprefix("尺寸 ")


def _size_in_diff(text: str) -> str:
    seg = next(s for s in _diff_line(text).split("；") if "尺寸：" in s)
    return seg.split("尺寸：")[1].split(" vs ")[0]


def test_comparison_line_size_matches_block_exactly(db_session: Session):
    """**逐字相等**，不是「包含某个数字」。

    审查 P1-3 实锤：此前的种子每个车系只造 1 个款型 1 条尺寸（单档、无覆盖率后缀），
    于是「走 `size_lines`」与「走 `heads_map` 首值」两种实现**输出完全相同**——
    变异体退回首值时全量测试 0 红，本轮声称修好的「三展示点同口径」**没有任何测试保护**。
    后果可复现：小米SU7 会变成「块里 1445 mm（在售 3 款中 2 款为此尺寸）、对比行 1460 mm」。
    """
    a, ba = _seed_size_tier(db_session, "甲车", "甲厂",
                             ["4997*1963*1460", "4997*1963*1445", "4997*1963*1445"])
    b, bb = _seed_size_tier(db_session, "乙车", "乙厂",
                             ["4900*1950*1520", "4900*1950*1500", "4900*1950*1500"])
    c, bc = _seed_size_tier(db_session, "丙车", "丙厂",
                             ["5050*1970*1540", "5050*1970*1520", "5050*1970*1520"])
    text = build_series_qa_answer(db_session, [(a, ba), (b, bb), (c, bc)], "甲车乙车丙车怎么选")

    for name, block_name in (("甲车", "甲厂甲车"), ("乙车", "乙厂乙车"), ("丙车", "丙厂丙车")):
        block = _size_in_block(text, block_name)
        assert "在售 3 款中 2 款为此尺寸" in block, f"{name} 未取众数：{block}"
    # 逐项对比行里的三个尺寸，必须与各自车系块**逐字相同**
    diff_sizes = [s.strip() for s in _diff_line_full(text)]
    blocks = [
        _size_in_block(text, "甲厂甲车"),
        _size_in_block(text, "乙厂乙车"),
        _size_in_block(text, "丙厂丙车"),
    ]
    assert diff_sizes == blocks, f"对比行 {diff_sizes} != 车系块 {blocks}"


def _diff_line(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith("同量纲参数对比"))


def _diff_line_full(text: str) -> list[str]:
    """返回「尺寸」那一项按 vs 切开的各车系取值（保持原样，不 strip）。"""
    seg = next(s for s in _diff_line(text).split("；") if "尺寸：" in s)
    return seg.split("尺寸：")[1].split(" vs ")



