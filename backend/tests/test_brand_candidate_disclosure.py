"""裸品牌作为比较候选时的反问：什么该报、什么**绝对不能**报（2026-10-06）。

用户拍板的口径只有一条：**按比较连接词切段、剥掉每段末尾的问句尾巴之后，品牌词与
整段完全相等**。其余一律不报——少一句提示的代价，远小于对着问奔驰 AMG 的用户推销
MG4/MG5/MG6。

这个文件是五轮审查的产物。前四轮都在**同一个问题**上出新洞，第五轮换掉了整个抽象：
从「重建用户写了什么、建区间、看品牌词是否落在所有区间之外」换成现在的
「遮蔽 + 切段 + 剥尾巴 + 整段相等」。换的根因写在
`app/catalog/brands.py::brand_candidates_in_message` 的 docstring 里，这里只记
**这个口径在真实库上守不守得住**。

关键点，缺一个都不算修好：

1. **追加，不是替换**。第一版做成替换，朗逸的 807 字参数卡被整段丢掉只剩一句
   反问——用户点名两台，一台数据也没了。所以每条正例都断言「已答对的那台车仍在」。
2. **三处调用点都要追加**。单车系 / 双车系 / ≥3 车系。
3. **切段字符不能劈进品牌词**。「比亚迪」含「比」（「对比」贡献的单字）——不遮蔽的话
   它被切成 `['比','亚迪','汉']`，库里车系最多的品牌之一（34 款在售）**永久不可达**。
   见 `test_brand_name_containing_link_char_stays_reachable`。
4. **不该报的一串，一个都不能报**。见 `test_must_not_report`。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.series_qa import _brand_active_series, build_series_qa_answer
from app.catalog.brands import brand_candidates_in_message
from app.catalog.series_index import resolve_series
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _ask(db: Session, msg: str) -> tuple[list, str]:
    resolved = resolve_series(db, msg)
    return resolved, build_series_qa_answer(db, resolved, msg)


def _seed(db: Session) -> None:
    """种子刻意造出**正例与反例都有**的形态，参数与真实库结构对齐。"""
    src = make_source(db, name="汽车之家")
    gq = make_brand(db, name="传祺", source=src)
    vw = make_brand(db, name="大众", source=src)
    benz = make_brand(db, name="奔驰", source=src)
    # 空品牌行：车全挂在「名爵」下（真实库 MG/名爵 就是这个形状）
    make_brand(db, name="MG", source=src)
    mg = make_brand(db, name="名爵", source=src)
    # 空品牌行 + 车系名以它开头，但车挂在**另一个**品牌行下（真实库 长安启源/启源）
    make_brand(db, name="星芒", source=src)
    other = make_brand(db, name="远航", source=src)
    far = make_brand(db, name="远大", source=src)

    langyi = make_series(db, vw, name="朗逸", source=src, positioning="紧凑型车")
    xuanyi = make_series(db, vw, name="轩逸", source=src, positioning="紧凑型车")
    maiteng = make_series(db, vw, name="迈腾", source=src, positioning="中型车")
    make_series(db, benz, name="奔驰C级AMG", source=src, positioning="紧凑型车")
    make_series(db, mg, name="MG4", source=src, positioning="紧凑型车")
    make_series(db, gq, name="传祺GS4", source=src, positioning="紧凑型SUV")
    # 星芒S7 挂在两个不同品牌行下 —— 用来钉「兜底只认同一品牌行」那条限制
    make_series(db, other, name="星芒S7", source=src, positioning="中型SUV")
    make_series(db, far, name="星芒L9", source=src, positioning="中型SUV")
    for s in (langyi, xuanyi, maiteng):
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db.commit()


# ── 正例：该反问 ─────────────────────────────────────────────────────────────


def test_single_series_appends_brand_ask(db_session: Session) -> None:
    """单车系路径：正常回答之后**追加**品牌反问，已答对的那台车仍在。

    真实问句「大众和汉哪个好」——大众有 29 款在售，它不是一款车。
    """
    _seed(db_session)
    resolved, text = _ask(db_session, "传祺和朗逸哪个好")
    assert len(resolved) == 1 and resolved[0][0].name == "朗逸"
    assert "款在售车" in text and "想比哪一款" in text, f"应反问。实际末尾：{text[-160:]}"
    assert "朗逸" in text, f"查得到的那台车不能被这段反问吞掉。实际末尾：{text[-160:]}"


def test_second_candidate_brand_is_detected_by_own_segment(db_session: Session) -> None:
    """品牌排在**第二个**时，它自己独占一段：「朗逸和传祺哪个好」。

    问句末尾通常跟着「哪个好」，所以第二个候选判不出来不是因为「右边不是连接词」，
    而是因为整段里混着别的东西——整段相等要求它单独成段。
    """
    _seed(db_session)
    _, text = _ask(db_session, "朗逸和传祺哪个好")
    assert "款在售车" in text, f"第二个候选的品牌也该反问。实际末尾：{text[-160:]}"


def test_brand_name_containing_link_char_stays_reachable(db_session: Session) -> None:
    """品牌名里含切段字符时，**必须仍然可达**。

    真实库：「比亚迪」含「比」——「对比」贡献了两个单字切段字符。不遮蔽的话
    「比亚迪和汉哪个好」被切成 `['比', '亚迪', '汉']`，**库里车系最多的品牌之一
    （34 款在售）永久不可达**，而且没有任何测试或文档提到它。

    构造同构数据：品牌「星比」含切段字「比」，车系挂在它名下。
    """
    src = make_source(db_session, name="汽车之家")
    vw = make_brand(db_session, name="大众", source=src)
    holder = make_brand(db_session, name="远望", source=src)
    weird = make_brand(db_session, name="星比", source=src)   # 品牌名含切段字「比」
    s = make_series(db_session, weird, name="星比007", source=src, positioning="轿车")
    langyi = make_series(db_session, vw, name="朗逸", source=src, positioning="紧凑型车")
    for x in (s, langyi):
        year = make_year(db_session, x)
        make_variant(db_session, x, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db_session.commit()

    assert brand_candidates_in_message(db_session, "星比和朗逸哪个好") == {"星比"}
    _, text = _ask(db_session, "星比和朗逸哪个好")
    assert "款在售车" in text, f"品牌名含切段字时不该失能。实际末尾：{text[-160:]}"
    assert "朗逸" in text
    del holder


def test_comma_separated_candidates_are_detected(db_session: Session) -> None:
    """逗号也是切段字符：「预算20万，大众和汉哪个好」这类写法非常常见。

    此前刻意把「，」排除在切段字符之外，理由写的是「汉的油耗怎么样，理想一点吗」
    里的「理想」会误报——那是推演没实测。实测：加逗号后该句切出
    `['汉的油耗怎么样', '理想一点吗']`，剥掉「吗」是「理想一点」≠「理想」，不报。
    见 `test_must_not_report` 里那条反例。
    """
    _seed(db_session)
    # 用传祺而不是大众：朗逸本身就是大众的车，会被 resolved_brands 相减挡掉
    for msg in ("预算20万，传祺和朗逸哪个好", "朗逸，传祺哪个好", "朗逸,传祺哪个好"):
        _, text = _ask(db_session, msg)
        assert "款在售车" in text, f"「{msg}」该反问却漏了。实际末尾：{text[-160:]}"
        assert "朗逸" in text


@pytest.mark.parametrize(
    ("msg", "same_class"),
    [
        ("传祺和朗逸、轩逸哪个好", True),     # 同级
        ("传祺和朗逸、迈腾哪个好", False),    # 异级
        ("传祺和朗逸、轩逸、迈腾哪个好", None),  # ≥3 走汇总
    ],
)
def test_all_three_call_sites_append(db_session: Session, msg: str, same_class: bool | None) -> None:
    """三处调用点都要追加。缩进错位过一次，且现有种子走不到那条分支。"""
    _seed(db_session)
    resolved, text = _ask(db_session, msg)
    assert len(resolved) >= 2, f"前提是多车系，实际 {[s.name for s, _ in resolved]}"
    if same_class is True:
        assert resolved[0][0].positioning == resolved[1][0].positioning == "紧凑型车", (
            f"前提是两台同级，实际 {[s.positioning for s, _ in resolved]}"
        )
    assert "款在售车" in text and "想比哪一款" in text, (
        f"「{msg}」这条路径没有追加品牌反问。实际末尾：{text[-200:]}"
    )
    for series, _b in resolved:
        assert series.name in text, f"「{series.name}」被吞掉了。实际末尾：{text[-200:]}"


def test_phantom_brand_row_is_rescued_by_name_prefix(db_session: Session) -> None:
    """空的重复品牌行要靠「车系名以该品牌词开头」兜住。

    真实库：「MG」行 0 款在售，7 款 MG 车全挂在「名爵」行下。不兜的话，
    「MG 和汉哪个好」会**一个字不提 MG**——而用户明明点名了它。
    """
    _seed(db_session)
    assert _brand_active_series(db_session, "MG") == ["MG4"], "空品牌行应被兜住"
    _, text = _ask(db_session, "MG和朗逸哪个好")
    assert "MG" in text and "款在售车" in text, f"「MG」不该被整段吞掉。实际末尾：{text[-160:]}"


# ── 反例：一个都不能报 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("msg", "why"),
    [
        ("奔驰C级AMG值得买吗", "品牌词嵌在完整车系名里"),
        ("C级AMG值得买吗", "裸型号写法（真实库实测 19 个车系曾全部中招）"),
        ("奔驰C级AMG和朗逸哪个好", "车型名以品牌词结尾、后面紧跟连接词"),
        ("朗逸和奔驰C级AMG哪个好", "同上，品牌词在中间"),
        ("星芒S7值得买吗", "品牌词嵌在别的品牌的车系名里"),
        ("朗逸和星芒S7哪个好", "同上"),
        ("朗逸适合大众家用吗", "「大众」是「普通人」"),
        ("朗逸的油耗怎么样，传祺一点吗", "「传祺」是副词，前面的「，」刻意不算连接词"),
        ("大众和朗逸哪个好", "朗逸就是大众的车，用户已点名到车系"),
        ("星芒和朗逸哪个好", "「星芒」是空品牌行，按 0 款不反问（数据侧空洞）"),
    ],
)
def test_must_not_report(db_session: Session, msg: str, why: str) -> None:
    """这些一律**不报**。其中「奔驰C级AMG和朗逸哪个好」是 908 全量扫出来的。"""
    _seed(db_session)
    resolved, text = _ask(db_session, msg)
    assert "想比哪一款" not in text, f"「{msg}」不该出现品牌反问（{why}）。实际末尾：{text[-200:]}"
    if resolved:
        for series, _b in resolved:
            assert series.name in text, f"「{msg}」把已答对的车弄丢了。"


def test_brand_of_resolved_series_is_subtracted(db_session: Session) -> None:
    """「大众和朗逸哪个好」：朗逸的品牌就是大众，反问等于把用户刚给的车型名推荐回去。

    挡住它的是 `resolved_brands` 相减，不是连接词判定——这两条是独立机制。
    """
    _seed(db_session)
    candidates = brand_candidates_in_message(db_session, "大众和朗逸哪个好")
    assert "大众" in candidates, "「大众」后面跟着「和」，连接词判定本身应该认出来"
    _, text = _ask(db_session, "大众和朗逸哪个好")
    assert "想比哪一款" not in text, f"应由 resolved_brands 相减挡掉。实际末尾：{text[-200:]}"


# ── 兜底路径的边界 ───────────────────────────────────────────────────────────


def test_prefix_fallback_needs_single_brand_row(db_session: Session) -> None:
    """前缀兜底**只认同一个品牌行**下的车系。

    「星芒」是空品牌行；「星芒S7」挂在**另一个**品牌「远航」下——那是另一家厂商的车，
    不该被算成「星芒」的。跨品牌行时兜底返回空，于是「星芒」既不算品牌、
    也不出现在回答里，**静默丢弃**——这条分支失败时不响，所以必须有测试钉住。
    """
    _seed(db_session)
    assert _brand_active_series(db_session, "星芒") == [], "跨品牌行时不该兜底"
    _, text = _ask(db_session, "星芒和朗逸哪个好")
    assert "星芒" not in text, f"不该把远航的车说成星芒的。实际末尾：{text[-200:]}"
    assert "朗逸" in text
