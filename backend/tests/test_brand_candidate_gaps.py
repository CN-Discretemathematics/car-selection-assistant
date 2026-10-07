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


# ── 数据不变量：切段字符不能把车系名/品牌名切出「恰好等于品牌词」的碎片 ─────


#: 探测用的字符池：正则 pattern 里出现的全部字符 + 常见可能作分隔符的字。
_PUNCT_POOL = " \t-—–·。、，,：:；;！!？?/／\\()（）【】[]{}「」『』…~～_"


def _real_cut_chars() -> set[str]:
    """**实测**哪些字符真能切开字符串——不从正则 pattern 里直接取字符集。

    上一版写成 `set(pattern) - set("[]()-")`，那是错的：`\\s` 让 `s` 混进来、
    交替符 `|` 与转义 `\\` 也混进来（都不是切段字符），而真正的切段字符
    `( ) -` 反而被一并减掉了。双向都错（审查实测）。

    改成「逐个字符试切，切得开的才算」，两个方向的错都自然消失。
    """
    pool = set(_BRAND_SEGMENT_CUT_RE.pattern) | set(_PUNCT_POOL)
    return {
        ch for ch in pool
        if ch not in "[]()^$*+?{}|" and len(re.split(_BRAND_SEGMENT_CUT_RE, "a" + ch + "b")) > 1
    }


def test_no_brand_name_contains_a_segment_cut_char(db_session: Session) -> None:
    """品牌词里不能含切段字符——含了就会被切段劈开。

    上一版这条测试**没有调用 `_seed`**，在空品牌表上断言，`not offenders` 恒成立，
    **任何 CI 环境下都不可能变红**。现已补上种子。
    """
    _seed(db_session)
    cut = _real_cut_chars()
    assert cut, "切段字符集不能是空的，否则这个不变量等于没有"
    offenders = sorted({
        (name, label) for name, _bid, label in _load_entries(db_session)
        if any(ch in name for ch in cut)
    })
    assert not offenders, (
        f"品牌词里含切段字符 {sorted(cut)}，会被切段劈开：{offenders}。"
        f"新方案不再遮蔽，请先把它加进 `_load_entries` 的别名或换个写法。"
    )


def test_no_series_fragment_equals_a_brand(db_session: Session) -> None:
    """**这才是真正的不变量**：车系名被切段切出的碎片，不该被当成独立品牌。

    2026-10-07 独立审查实测的一类真误反问：切段字符把**含空格/连字符的车系名**
    切碎，碎片恰好等于某个品牌词——「MG Cyberster和汉哪个好」在答案末尾追加
    「MG 是品牌，库里有 7 款…它不是一款车」，而答案开头刚报完这台车的完整参数。
    真实库 18 个车系中招（MG 4X / MG Cyberster / MG ES5 / iCAR 超级V23 / iCAR V27 /
    极狐 阿尔法S5 / 极狐 考拉S …）。

    这里用**合成数据**造一个违反样本（车系名里带切段字符 + 该字符前半截又是品牌词），
    断言**端到端**（`_brand_disclosure`，兜底就在那一层）不反问。比「拿真实库现状
    当不变量」可靠：将来谁往切段集里加字符、或库里新增这种车系名，这里会先红。
    """
    _seed(db_session)
    src = make_source(db_session, name="汽车之家")
    holder = make_brand(db_session, name="某某厂", source=src)
    make_brand(db_session, name="零光", source=src)      # 与下一台车系名同形
    make_series(db_session, holder, name="零光-01", source=src, positioning="轿车")
    db_session.commit()

    from app.agent.series_qa import _brand_disclosure
    from app.catalog.series_index import resolve_series

    msg = "零光-01值得买吗"
    resolved = resolve_series(db_session, msg)
    assert resolved and resolved[0][0].name == "零光-01", "前提：车系名能解析出来"
    assert "-" in _real_cut_chars(), "前提：连字符是切段字符"
    assert _brand_disclosure(db_session, resolved, msg).strip() == "", (
        "车系名「零光-01」被切段切出「零光」，而「零光」又是品牌词——"
        "这正是 MG Cyberster / iCAR 超级V23 那一类误反问的形状"
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


def test_punctuation_separated_candidates(db_session: Session) -> None:
    """顿号 / 逗号 / 连字符是切段字符——这些**不可能出现在车系名里**，是纯收益。"""
    _seed(db_session)
    for q in ("大众、丰田和汉哪个好", "大众-丰田和汉哪个好", "大众，丰田和汉哪个好",
              "大众,丰田和汉哪个好"):
        got = brand_candidates_in_message(db_session, q)
        assert {"大众", "丰田"} <= got, f"「{q}」应报出大众与丰田。实得={sorted(got)}"


def test_space_is_not_a_cut_char(db_session: Session) -> None:
    """**空格刻意不是切段字符**——真实库有 18 个车系名里带空格。

    把空格放进切段集，切出的碎片恰好等于品牌词：
        MG Cyberster → ['MG', 'Cyberster']     → 「MG」是品牌，7 款
        iCAR 超级V23 → ['iCAR', '超级V23']      → 「iCAR」是品牌
        极狐 阿尔法S5 → ['极狐', '阿尔法S5']    → 「极狐」是品牌
    用户问一台真车，却被告知「它不是一款车」。代价是「大众 丰田」这类空格并列不再切——
    那是**漏报**方向，安全。

    端到端的兜底在 `test_no_series_fragment_equals_a_brand` 与
    `test_space_separated_series_does_not_leak_a_brand`。
    """
    assert " " not in _real_cut_chars(), "空格不能是切段字符（真实库 18 个车系名带空格）"
    assert "\t" not in _real_cut_chars(), "制表符同理"


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


def test_space_separated_series_does_not_leak_a_brand(db_session: Session) -> None:
    """真实库里**带空格的车系名**问一遍，端到端不得多出品牌反问。

    照搬 2026-10-07 独立审查实测中招的三个车系形状：品牌词恰好是车系名的前半截。
    """
    _seed(db_session)
    src = make_source(db_session, name="汽车之家")
    holder = make_brand(db_session, name="某某汽车", source=src)
    for brand_name in ("极光", "银河"):
        make_brand(db_session, name=brand_name, source=src)
    for sname in ("极光 星舰", "银河 E5", "极光007"):
        make_series(db_session, holder, name=sname, source=src, positioning="轿车")
    db_session.commit()

    from app.agent.series_qa import _brand_disclosure
    from app.catalog.series_index import resolve_series

    for msg in ("极光 星舰值得买吗", "银河 E5值得买吗", "极光 星舰和银河 E5哪个好"):
        resolved = resolve_series(db_session, msg)
        assert resolved, f"前提：「{msg}」能解析出车系"
        note = _brand_disclosure(db_session, resolved, msg).strip()
        assert not note, f"「{msg}」不该报出品牌反问（车系名带空格，前半截恰是品牌词）。实得：{note[:120]}"


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
