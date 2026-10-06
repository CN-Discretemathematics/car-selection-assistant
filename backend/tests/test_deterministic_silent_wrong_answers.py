"""部署态两处「静默给出错误答案」的回归（2026-10-05 独立审查 P1）。

两处都不是语义错误，而是**确定性层**做错了——按 AGENTS.md 的诊断判据：
答案长得自信、规整、有数据 → 就是确定性链路，与模型无关。
实测（`DEEPSEEK_API_KEY=''`、模型完全禁用）错误答案照样出现。

1) `_MULTI_VALUE_RE` 漏了 `~`：注释自己举的例子 `29.165~74.96/75.26` 就含它，
   只是顺带含 `/` 才被拦下。没有 `/` 时会拼出下端点本身是区间的乱码。
   旧测试把脏值**人工拆成两条事实**，于是 `~` 从未被检验过。
2) 单字车系名「汉」「炮」缺构词拦截：「汉字续航是多少」「汉朝的车值得买吗」
   「炮灰的车怎么样」「武汉」全部解析出车系，并走完 series_qa 输出**完整参数卡**。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.catalog.series_index import (
    _MULTI_VALUE_RE,
    _single_char_blocked_spans,
    normalize_name,
    rank_headlines,
    resolve_series,
)
from tests.seed import make_brand, make_series, make_source


def _seed(db: Session):
    src = make_source(db, name="汽车之家")
    byd = make_brand(db, name="比亚迪", source=src)
    gwm = make_brand(db, name="长城", source=src)
    han = make_series(db, byd, name="汉", body_type="sedan",
                      energy_types=("BEV",), source=src)
    pao = make_series(db, gwm, name="炮", body_type="pickup",
                      energy_types=("BEV", "ICE"), source=src)
    db.commit()
    return han, pao


# ── 1) `~` 必须是多值分隔符 ────────────────────────────────────────────────

def test_multi_value_re_keeps_garbage_slash_separated():
    """真正的垃圾是**斜杠**：端点整串，挑不出该取哪个当上下界。

    2026-10-06 的一次错误在这里留了记录：独立审查建议把 `~` 也当分隔符，
    照做后回真实库一量——**全库 23 条含 `~` 的 fact_value 全是合法区间**
    （最大扭矩转速 1500~2400、厂商指导价 4.46万~4.49万），而代码自己就用 `~`
    拼输出格式。把它当分隔符等于把 23 条真数据全打成单值。已撤回。
    """
    assert _MULTI_VALUE_RE.search("29.165~74.96/75.26"), "斜杠脏数据必须被拦下"


def test_legitimate_tilde_ranges_are_not_filtered():
    """合法区间**不得**被当成多值串退回单值——本文件最重要的回归防护。

    两条数据都取自真实库实测（2026-10-06，全库 23 条含 `~` 的值全是这一类）：
    最大扭矩转速 1500~2400、厂商指导价 4.46万~4.49万。
    """
    for legit in ("1500~2400", "4.46万~4.49万", "100~200"):
        assert not _MULTI_VALUE_RE.search(legit), (
            f"合法区间 {legit!r} 被误判成多值串 → 会退回单值，真实数据静默降级"
        )


def test_multi_value_re_still_catches_real_garbage():
    """撤回 `~` 不等于放松：斜杠类脏数据仍必须拦下。"""
    facts = {1: [("电池能量(kWh)", "29.165~74.96/75.26", "kWh", None),
                 ("电池能量(kWh)", "100", "kWh", None)]}
    result = rank_headlines(facts)
    value = result[1]["电池"]
    assert "/" not in value and "~74.96" not in value, (
        f"下端点本身是区间的乱码被拼进来了：{value!r}"
    )


# ── 2) 单字车系名的构词拦截 ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "msg",
    [
        "汉字续航是多少",
        "汉朝的车值得买吗",
        "汉族",
        "武汉",
        "炮灰的车怎么样",
        "大炮什么时候能买",
        "炮弹的射程",
    ],
)
def test_single_char_series_not_matched_inside_common_words(
    db_session: Session, msg: str
):
    """常见词里的「汉」「炮」不得被当成车系名。"""
    _seed(db_session)
    assert resolve_series(db_session, msg) == [], (
        f"「{msg}」解析出了车系 {[s.name for s, _ in resolve_series(db_session, msg)]}，"
        "会输出一张自信的参数卡，但用户问的根本不是车"
    )


@pytest.mark.parametrize(
    "msg",
    ["汉怎么样", "汉的续航多少", "汉的售价呢", "炮怎么样", "炮能拉货吗", "汉L和汉怎么选"],
)
def test_single_char_series_still_resolves_in_real_questions(
    db_session: Session, msg: str
):
    """反向保护：拦构词不能把真实购车问法一起拦掉。"""
    _seed(db_session)
    names = [s.name for s, _ in resolve_series(db_session, msg)]
    assert names, f"「{msg}」本该解析出车系，却一个都没有"


def test_blocked_spans_cover_prefix_and_suffix_compounds():
    """禁构词要同时处理「汉」在**前**（汉字）与在**后**（武汉）两种位置。"""
    assert _single_char_blocked_spans(normalize_name("汉字"), "汉") == [(0, 2)]
    assert _single_char_blocked_spans(normalize_name("武汉"), "汉") == [(0, 2)]
    assert _single_char_blocked_spans(normalize_name("汉怎么样"), "汉") == []
