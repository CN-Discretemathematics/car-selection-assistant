"""补做「本轮不做」里已拍板修掉的三项（2026-10-07 用户拍板）。

1. **点名超过上限的车系不能被静默丢掉**——此前 `resolve_series` 硬编码 `[:4]`，
   点名 5 台时第 5 台无声消失，用户以为 5 台都参与了比较。与同批修的「品牌被整段吞掉」
   是同一类错误：不响。
2. **销量榜主句式接不住**——「什么车销量最好」此前落到错分支去（`销量` 只在
   `销量(榜|排名|排行)` 一个分支里）。**刻意不补裸 `销量(最好|最高)`**：两品牌对比
   （「大众和丰田哪个销量好」）里 `resolved` 为空，裸匹配会把它变成「给一张 Top10」。
3. **尺寸覆盖率的分母写错了**——`size_lines` 的 `total` 是**有尺寸事实的款数**，
   文案却写「在售 N 款」，于是「在售 6 款中 1 款为此尺寸」会被读成「6 款里 5 款尺寸不对」，
   而真相是另外 5 款**尺寸未披露**。数字没错，框架误导。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.routing import asks_sales_ranking
from app.agent.series_qa import build_series_qa_answer
from app.catalog.series_index import (
    RESOLVE_SERIES_LIMIT,
    resolve_series,
    resolve_series_with_dropped,
)
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def test_limit_constant() -> None:
    """上限是 6（2026-10-07 由 4 提到 6）。放在函数里而不是模块级——模块级 assert
    失败会变成**收集错误**，看不出是哪条测试红的。"""
    assert RESOLVE_SERIES_LIMIT == 6, "上限是 6；改成别的值时下面几条断言要一起改"


def _seed(db: Session) -> None:
    """造 8 台彼此定位不同的车系，够点出「超过上限」的情形。"""
    src = make_source(db, name="汽车之家")
    vw = make_brand(db, name="大众", source=src)
    names = [
        ("朗逸", "紧凑型车"), ("轩逸", "紧凑型车"), ("迈腾", "中型车"),
        ("途安", "紧凑型MPV"), ("桑塔纳", "紧凑型车"), ("帕萨特", "中型车"),
        ("POLO", "小型车"), ("高尔夫", "紧凑型车"),
    ]
    for name, pos in names:
        s = make_series(db, vw, name=name, source=src, positioning=pos)
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db.commit()


def _seed_size_tiers(db: Session) -> None:
    """造一个**尺寸多档、众数 ≠ 首值**的车系——正是覆盖率后缀会出现的形状。

    只造 1 个款型 1 条尺寸的话是单档，走不到带后缀的分支（本仓已吃过一次这个亏，
    见 `test_multi_series_compare.py:266` 的注释）。
    """
    src = make_source(db, name="汽车之家")
    b1 = make_brand(db, name="甲厂", source=src)
    b2 = make_brand(db, name="乙厂", source=src)
    for brand, tag, sizes in ((b1, "甲", ["4997*1963*1460", "4997*1963*1445", "4997*1963*1445"]),
                             (b2, "乙", ["4900*1950*1520", "4900*1950*1500", "4900*1950*1500"])):
        series = make_series(db, brand, f"{tag}车", source=src, positioning="中大型车")
        for i, dim in enumerate(sizes, 1):
            year = make_year(db, series, year_name=f"2025款{tag}{i}")
            make_variant(db, series, year, config_version=f"配置{i}", energy_type="BEV",
                         price_cny=str(100000 + i * 1000), source=src,
                         facts=[("尺寸", "长*宽*高(mm)", dim, "mm", None)])
    db.commit()


# ── ① 点名超过上限：上限提到 6，且**明说**被截掉哪几台 ────────────────────


def test_under_limit_nothing_dropped(db_session: Session) -> None:
    """没超过上限时，一个字都不多说。"""
    _seed(db_session)
    msg = "朗逸、轩逸、迈腾哪个好"
    resolved = resolve_series(db_session, msg)
    assert len(resolved) == 3
    _, dropped = resolve_series_with_dropped(db_session, msg)
    assert dropped == []
    assert "没有放进来" not in build_series_qa_answer(db_session, resolved, msg)


def test_exactly_at_limit_nothing_dropped(db_session: Session) -> None:
    """正好等于上限（6 台）时不披露——没丢东西就不该说有丢东西。"""
    _seed(db_session)
    msg = "朗逸、轩逸、迈腾、途安、桑塔纳、帕萨特哪个好"
    resolved = resolve_series(db_session, msg)
    assert len(resolved) == RESOLVE_SERIES_LIMIT
    _, dropped = resolve_series_with_dropped(db_session, msg)
    assert dropped == []
    assert "没有放进来" not in build_series_qa_answer(db_session, resolved, msg)


def test_over_limit_discloses_dropped_names(db_session: Session) -> None:
    """超过上限时，**明说**哪几台没放进来——这正是 2026-10-07 要治的静默丢弃。"""
    _seed(db_session)
    msg = "朗逸、轩逸、迈腾、途安、桑塔纳、帕萨特、POLO、高尔夫哪个好"
    resolved = resolve_series(db_session, msg)
    assert len(resolved) == RESOLVE_SERIES_LIMIT
    _, dropped = resolve_series_with_dropped(db_session, msg)
    assert dropped == ["POLO", "高尔夫"]
    text = build_series_qa_answer(db_session, resolved, msg)
    assert "没有放进来" in text, f"超上限必须明说。实际末尾：{text[-260:]}"
    assert "POLO" in text and "高尔夫" in text
    assert "你一共提到 8 台车" in text
    # 已放进来的车一台都不能少
    for name in ("朗逸", "轩逸", "迈腾", "途安", "桑塔纳", "帕萨特"):
        assert name in text, f"「{name}」被吞掉了"


def test_dropped_note_not_shown_when_resolution_differs(db_session: Session) -> None:
    """传进来的 `resolved` 与重算结果不一致时**不披露**——不拿可能对不上的名单糊弄用户。"""
    _seed(db_session)
    msg = "朗逸、轩逸、迈腾、途安、桑塔纳、帕萨特、POLO、高尔夫哪个好"
    full = resolve_series(db_session, msg)
    tampered = full[:-1]          # 人为砍掉一台，模拟调用方给的 resolved 与消息不符
    text = build_series_qa_answer(db_session, tampered, msg)
    assert "没有放进来" not in text, f"resolved 对不上时不该披露。实际末尾：{text[-260:]}"


# ── ② 销量榜词表：主句式接住，两品牌对比不被抢 ────────────────────────────


def test_sales_ranking_main_phrasings() -> None:
    """用户最常问的这几句此前接不住（`销量` 只在 `销量(榜|排名|排行)` 里）。"""
    for q in (
        "什么车销量最好", "什么车卖得最好", "什么车销量最高", "什么车卖得最多",
        "哪款车销量最高", "哪台车销量最好", "销量第一的是谁", "销量榜单",
        "卖得最多的是哪款", "本月销量榜", "什么车最受欢迎", "热门车有哪些",
    ):
        assert asks_sales_ranking(q), f"「{q}」应判为销量榜问句"


def test_sales_ranking_does_not_swallow_comparisons() -> None:
    """**不补裸 `销量(最好|最高)` 的理由**：两品牌/两车对比不该被抢成榜单问句。

    「大众和丰田哪个销量好」里两个都是品牌，`resolved` 为空——`not resolved`
    那道判断挡得住车系、**挡不住品牌**，裸匹配会把它变成「给一张 Top10」。
    """
    for q in (
        "大众和丰田哪个销量好", "朗逸和汉哪个销量高", "汉的销量怎么样",
        "星愿的销量如何", "大众销量比丰田高吗",
    ):
        assert not asks_sales_ranking(q), f"「{q}」是对比问句，不该被判成全库榜单"


# ── ③ 尺寸覆盖率分母：说清分母是什么 ──────────────────────────────────────


def test_size_coverage_states_what_the_denominator_is(db_session: Session) -> None:
    """分母是**有尺寸事实的款数**，文案必须这么写，不能写「在售 N 款」。

    「在售 6 款中 1 款为此尺寸」会被读成「6 款里 5 款尺寸不对」，
    而真相是另外 5 款**尺寸未披露**。数字没错，框架误导。
    """
    _seed_size_tiers(db_session)
    a, ab = resolve_series(db_session, "甲车")[0]
    b, bb = resolve_series(db_session, "乙车")[0]
    text = build_series_qa_answer(db_session, [(a, ab), (b, bb)], "甲车和乙车怎么选")
    assert "有尺寸数据" in text, f"分母口径要写清。实际：{text[:500]}"
    assert "其中 2 款为此尺寸" in text, f"众数 2 款要照实写。实际：{text[:500]}"
    assert "在售 3 款中" not in text, "不应再出现把「有尺寸数据的款数」说成「在售款数」"


def test_size_single_tier_has_no_coverage_suffix(db_session: Session) -> None:
    """单档尺寸不写覆盖率后缀——此前就如此，本次不动。"""
    _seed(db_session)
    resolved = resolve_series(db_session, "朗逸、轩逸哪个好")
    text = build_series_qa_answer(db_session, resolved, "朗逸、轩逸哪个好")
    assert "有尺寸数据" not in text, f"单档不该加后缀。实际：{text[:400]}"
