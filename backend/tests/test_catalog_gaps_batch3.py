"""第 3 批（2026-10-07 用户拍板）：幻影品牌行借用 / 太泛的短名不注册 / 空列表不崩。

三项都是**如实回答「库里有什么」**方向的问题——用户点了一台车或一个品牌，系统得
说实话或说对，不能装看不见也不能认错。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.series_qa import _brand_active_series, build_series_qa_answer
from app.catalog.series_index import resolve_series
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> None:
    src = make_source(db, name="汽车之家")
    vw = make_brand(db, name="大众", source=src)
    for name, pos in (("朗逸", "紧凑型车"), ("轩逸", "紧凑型车"), ("迈腾", "中型车")):
        s = make_series(db, vw, name=name, source=src, positioning=pos)
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db.commit()


# ── ① 幻影品牌行：名字在、车不在，车挂在同公司另一个品牌行下 ─────────────────


def test_phantom_brand_row_borrows_sibling(db_session: Session) -> None:
    """品牌名**以另一品牌名开头或结尾**时，借那个品牌的车系。

    真实库的幻影行（2026-10-07 实测，改前全部 0 款）：
        吉利银河 0  ←→ 吉利汽车 11 / 银河 12
        零跑汽车 0  ←→ 零跑 9
        理想汽车 0  ←→ 理想 5
        AITO 问界 0 ←→ 问界 5
        待分类（汽车之家销量榜）0 ←→ 无对应，仍为 0
    「车系名以该品牌词开头」那条兜底救不了它们——零跑的车叫「零跑T03」而不是
    「零跑汽车T03」。
    """
    _seed(db_session)
    src = make_source(db_session, name="汽车之家")
    for phantom, sibling, sibling_series in (
        ("星月汽车", "星月", "星月S7"),
        ("银河星舰", "银河", "银河L7"),          # 幻影名以真品牌名**结尾**
    ):
        make_brand(db_session, name=phantom, source=src)      # 幻影行：不放车
        real = make_brand(db_session, name=sibling, source=src)
        make_series(db_session, real, name=sibling_series, source=src, positioning="轿车")
    db_session.commit()

    got = _brand_active_series(db_session, "星月汽车")
    assert got == ["星月S7"], f"「星月汽车」应借「星月」的车。实得={got}"
    got = _brand_active_series(db_session, "银河星舰")
    assert got == ["银河L7"], f"「银河星舰」应借「银河」的车。实得={got}"


def test_phantom_borrow_does_not_override_real_counts(db_session: Session) -> None:
    """借用只在**直接查不到**时发生，且有真车的品牌计数不能被兄弟行顶掉。"""
    _seed(db_session)
    assert _brand_active_series(db_session, "大众") == ["朗逸", "轩逸", "迈腾"]
    # 真车系名正好以品牌名开头的（长安启源式）仍走原有那条，不受借用影响
    src = make_source(db_session, name="汽车之家")
    parent = make_brand(db_session, name="星原", source=src)
    sub = make_brand(db_session, name="星原启航", source=src)     # 幻影行
    del sub
    make_series(db_session, parent, name="星原启航Q5", source=src, positioning="轿车")
    make_series(db_session, parent, name="星原启航L7", source=src, positioning="轿车")
    make_series(db_session, parent, name="星原S1", source=src, positioning="轿车")
    db_session.commit()
    assert _brand_active_series(db_session, "星原启航") == ["星原启航Q5", "星原启航L7"]


# ── ② 太泛的短名不注册 ─────────────────────────────────────────────────────


def test_too_generic_short_name_is_not_registered(db_session: Session) -> None:
    """字母数字混搭且短于 4 字符的「去品牌前缀短名」不注册。

    「问界M6」去掉品牌前缀得到「M6」，而「传祺M6」里就含「m6」→ 解析成问界M6
    （库里并没有传祺M6）。同类还有「宝马i5 M60」（"m60" 里含 "m6"）。
    这类短名是**跨品牌的通用型号代号**，不是某台车的名字。
    """
    _seed(db_session)
    src = make_source(db_session, name="汽车之家")
    benz = make_brand(db_session, name="车和", source=src)
    make_series(db_session, benz, name="车和Q5", source=src, positioning="轿车")
    db_session.commit()

    # 「车和」品牌下有个车系叫「车和Q5」→ 短名「Q5」被跳过
    assert resolve_series(db_session, "Q5值得买吗") == [], (
        "「Q5」是跨品牌通用型号代号，不该被当成「车和Q5」的简称"
    )
    assert resolve_series(db_session, "传祺Q5值得买吗") == []
    # 完整名与「品牌+车系」仍然照常
    assert [s.name for s, _ in resolve_series(db_session, "车和Q5值得买吗")] == ["车和Q5"]
    assert [s.name for s, _ in resolve_series(db_session, "车和车和Q5怎么样")] == ["车和Q5"]


def test_single_char_and_long_short_names_still_work(db_session: Session) -> None:
    """单字车系名（汉/炮）与较长的去前缀短名**不能**被这条规则误伤。"""
    _seed(db_session)
    src = make_source(db_session, name="汽车之家")
    byd = make_brand(db_session, name="某车", source=src)
    make_series(db_session, byd, name="某车汉", source=src, positioning="轿车")
    make_series(db_session, byd, name="某车星越L", source=src, positioning="轿车")
    db_session.commit()
    assert [s.name for s, _ in resolve_series(db_session, "汉怎么样")] == ["某车汉"]
    assert [s.name for s, _ in resolve_series(db_session, "星越L怎么样")] == ["某车星越L"]


# ── ③ 空列表不崩 ────────────────────────────────────────────────────────────


def test_empty_resolved_returns_empty_string(db_session: Session) -> None:
    """`build_series_qa_answer(db, [], msg)` 返回空串，**不再抛 IndexError**。

    此前双车系分支的 `resolved[0][0]` / `resolved[1][0]` 在空列表上直接崩。
    生产调用点有 `if resolved:` 挡着、用户碰不到，但只要将来多一个不经那层保护的
    调用点，或有人拿探针直接调它，就是一个必崩的入口。
    """
    _seed(db_session)
    assert build_series_qa_answer(db_session, [], "大众和比亚迪哪个好") == ""
    assert build_series_qa_answer(db_session, [], "") == ""


# ── ④ 关键回归守卫 ──────────────────────────────────────────────────────────


def test_normal_two_series_still_answered(db_session: Session) -> None:
    """加借用与空列表兜底后，正常路径不能被带偏。"""
    _seed(db_session)
    msg = "传祺和朗逸哪个好"
    resolved = resolve_series(db_session, msg)
    assert [s.name for s, _ in resolved] == ["朗逸"]
    text = build_series_qa_answer(db_session, resolved, msg)
    assert "朗逸" in text and len(text) > 50
