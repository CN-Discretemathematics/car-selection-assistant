"""品牌反问的漏报修复（2026-10-07 用户拍板 ⑥⑦⑨）。

三处都是**漏报**方向——用户提了品牌，系统不提示。修的方向必须始终是安全的：
**宁可漏报，不可误报**。本文件里有一批「不该报」的反例专门守这条线。

三处修法：

- **⑥ 用原始消息切段**。原先切的是 `normalize_name` 的结果，而 `_SEP_RE` 在归一化
  阶段就把「、」空格 `-` `.` `/` 括号等 15 种写法删了，切段根本看不到——实测
  「大众、丰田和本田哪个好」只报得出「本田」，且**只把顿号加进切段集是彻底 no-op**。
- **⑦ 剥段首的提问前缀**。「我想买大众和汉哪个好」原先整段是「我想买大众」，
  与品牌词不相等就白切了。
- **⑨ 扩问句尾巴**。第二候选位对尾巴极敏感，「哪个好」报、「哪个好一些」不报。
  ⚠️ **只收「问句形态」的复合尾巴，不收裸的程度副词**——收「一些/一点/更」实测
  立刻造出误报：段「理想一点」剥掉「一点」就塌成「理想」。
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.catalog.brands import (
    _BRAND_SEGMENT_CUT_RE,
    _load_entries,
    brand_candidates_in_message,
)
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> None:
    src = make_source(db, name="汽车之家")
    vw = make_brand(db, name="大众", source=src)
    make_brand(db, name="丰田", source=src)   # 并列用例要三家都在库里
    make_brand(db, name="本田", source=src)
    for name in ("朗逸", "轩逸", "汉"):
        s = make_series(db, vw, name=name, source=src, positioning="紧凑型车")
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db.commit()


# ── 数据不变量：新方案不遮蔽，切段字符劈进品牌名就会坏掉 ───────────────────


def test_no_brand_name_contains_a_segment_cut_char(db_session: Session) -> None:
    """**没有任何品牌词含切段字符**——这是「去掉遮蔽」能成立的前提。

    去掉遮蔽后，品牌词若含切段字符就会被劈开（「比亚迪」曾因含「比」而永久不可达）。
    真实库 95 个条目逐个查过是 0，但那是**数据现状、不是代码保证**——将来有人加
    一个带「和」「与」「/」的品牌名，这里会红。守住它，别等出事再查。
    """
    cut_chars = set(_BRAND_SEGMENT_CUT_RE.pattern) - set("[]()-")
    offenders = sorted({
        (name, label) for name, _bid, label in _load_entries(db_session)
        if any(ch in name for ch in cut_chars)
    })
    assert not offenders, (
        f"品牌词里含切段字符 {sorted(cut_chars)}，会被切段劈开：{offenders}。"
        f"新方案不再遮蔽，请先把它加进 `_load_entries` 的别名或换个写法。"
    )


def test_duibi_is_matched_before_its_single_chars() -> None:
    """「对比」必须作为**两字连写**先匹配，否则「比亚迪」会被劈成「比」「亚迪」。

    真实库有 34 款在售的比亚迪车型，前五轮审查里它曾因此永久不可达。
    """
    assert _BRAND_SEGMENT_CUT_RE.split("帮我对比比亚迪和汉的配置差异")[1] == "比亚迪"


# ── ⑥ 顿号 / 空格 / 连字符并列 ─────────────────────────────────────────────


def test_dun_hao_separated_candidates(db_session: Session) -> None:
    """「大众、丰田和本田哪个好」——三个并列候选都要报出来。

    改前只报得出「本田」：`_SEP_RE` 把顿号在归一化阶段就删了，切段看不到。
    """
    _seed(db_session)
    got = brand_candidates_in_message(db_session, "大众、丰田和本田哪个好")
    assert {"大众", "丰田", "本田"} <= got, f"顿号并列的三家都该报出。实得={sorted(got)}"


def test_space_and_hyphen_separated_candidates(db_session: Session) -> None:
    """空格与连字符同样是切段字符。"""
    _seed(db_session)
    for q in ("大众 丰田和汉哪个好", "大众-丰田和汉哪个好", "大众，丰田和汉哪个好"):
        got = brand_candidates_in_message(db_session, q)
        assert {"大众", "丰田"} <= got, f"「{q}」应报出大众与丰田。实得={sorted(got)}"


# ── ⑦ 段首提问前缀 ─────────────────────────────────────────────────────────


def test_leading_question_prefixes(db_session: Session) -> None:
    """「我想买大众和汉哪个好」这类前缀写法改前一律不报。"""
    _seed(db_session)
    for q in (
        "我想买大众和汉哪个好", "想买大众和汉哪个好", "要买大众和汉哪个好",
        "打算买大众和汉哪个好", "请问大众和汉哪个好", "那大众和汉哪个好",
        "预算20万大众和汉哪个好",
    ):
        got = brand_candidates_in_message(db_session, q)
        assert "大众" in got, f"「{q}」应报出大众。实得={sorted(got)}"


# ── ⑨ 问句尾巴 ────────────────────────────────────────────────────────────


def test_compound_question_tails(db_session: Session) -> None:
    """第二候选位：多一个字就整段作废，用**复合**问句尾巴补。"""
    _seed(db_session)
    for q in (
        "汉和大众哪个好一些", "汉和大众哪个更好", "汉和大众哪种好",
        "汉和大众哪个好呢", "汉和大众哪个好啊", "汉和大众哪款好",
    ):
        got = brand_candidates_in_message(db_session, q)
        assert "大众" in got, f"「{q}」应报出大众。实得={sorted(got)}"


def test_bare_degree_words_are_not_tails(db_session: Session) -> None:
    """**裸的程度副词不能当尾巴**——那会把「理想一点」塌成「理想」。

    收过「一些/一点/更」三个，实测立刻造出误报：
        「汉的油耗怎么样，理想一点吗」 [] → ['理想']
        「我想买理想一些吗」           [] → ['理想']
    拿误报换漏报，方向错了。这条守住它。
    """
    _seed(db_session)
    for q in (
        "汉的油耗怎么样，理想一点吗", "汉的油耗怎么样，理想一点",
        "我想买理想一些吗", "朗逸的油耗理想一点吗", "汉适合大众一些吗",
        "汉适合大众更好吗", "大众的家用性长安一些吗", "朗逸适合大众家用吗",
    ):
        got = brand_candidates_in_message(db_session, q)
        assert "理想" not in got and "大众" not in got and "长安" not in got, (
            f"「{q}」不该报出品牌。实得={sorted(got)}"
        )


# ── 回归守卫：改前就报、改后也报的都要保住 ─────────────────────────────────


def test_unchanged_positives_still_fire(db_session: Session) -> None:
    """本轮动的是切段方式与尾巴，收窄时最容易把**原本能报**的弄丢。"""
    _seed(db_session)
    for q in ("大众和汉哪个好", "汉和大众哪个好"):
        assert "大众" in brand_candidates_in_message(db_session, q), f"「{q}」"


def test_unchanged_negatives_stay_silent(db_session: Session) -> None:
    """品牌片段 / 自家车系 / 款型名——这些改前不报，改后也不能报。"""
    _seed(db_session)
    for q in (
        "大众朗逸和汉哪个好", "2025款 熊猫mini 210km 元气熊值得买吗",
        "朗逸适合大众家用吗", "汉的油耗怎么样，理想一点吗",
    ):
        got = brand_candidates_in_message(db_session, q)
        assert not got, f"「{q}」不该报出品牌。实得={sorted(got)}"


def test_brand_of_resolved_series_is_left_to_the_caller(db_session: Session) -> None:
    """「大众和朗逸哪个好」在**候选层**仍会报出大众——由调用方的 `resolved_brands`
    相减挡掉。这里守住的是「相减那一层还在工作」，不是「候选层不报」。"""
    _seed(db_session)
    from app.agent.series_qa import _brand_disclosure
    from app.catalog.series_index import resolve_series

    resolved = resolve_series(db_session, "大众和朗逸哪个好")
    assert "大众" in brand_candidates_in_message(db_session, "大众和朗逸哪个好")
    assert not _brand_disclosure(db_session, resolved, "大众和朗逸哪个好").strip()
