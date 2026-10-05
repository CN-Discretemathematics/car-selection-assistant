"""P0-2「座位未核实」的披露测试（2026-10-05 生产实测）。

生产实测形态：用户点「5 人以上」，5 款候选里 2 款查不到座位数，卡片只写
「预算匹配、用途匹配」，**只字不提座位那条根本没校验**。

本组钉住三件事：
1. `seat_verified` 逐款如实标记（用户没提人数时不得报未核实——那是噪音）。
2. `seat_check` 覆盖率汇总只在该报的时候报。
3. 顶部说明文案有具体数字，且覆盖率为 100% 时保持沉默。

⚠️ 这组测试是**补写的**：首轮反向验证发现 P0-2 一条测试都没有，
把 `seat_verified` 改成恒 `True` 全仓测试仍然全绿——即修复本身没有牙齿。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.engine import AgentEngine
from app.agent.schemas import Budget, UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> dict[str, int]:
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)
    out: dict[str, int] = {}
    for name, fact in (
        ("有座位", ("参数信息", "座位数(个)", "6", "个", None)),
        ("无座位", None),
    ):
        s = make_series(db, brand, name=name, body_type="suv", energy_types=("BEV",), source=source)
        y = make_year(db, s)
        out[name] = make_variant(
            db, s, y, price_cny="150000", energy_type="BEV",
            facts=[fact] if fact else [], source=source,
        ).id
    db.commit()
    return out


def _profile(**kw) -> UserProfile:
    p = UserProfile(budget=Budget(max=200000))
    p.usage = ["家庭"]
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_seat_verified_false_only_for_variant_without_seat_facts(db_session: Session):
    ids = _seed(db_session)
    result = recommendation_tool(db_session, _profile(passengers=6))
    flags = {v["variant_id"]: v["seat_verified"] for v in result["variants"]}
    assert flags[ids["无座位"]] is False, "查不到座位数的款型必须标未核实"
    assert flags[ids["有座位"]] is True


def test_seat_verified_all_true_when_user_never_mentioned_passengers(db_session: Session):
    """用户没提人数 → 本就无座位约束 → 不得报未核实（那是纯噪音）。"""
    _seed(db_session)
    result = recommendation_tool(db_session, _profile())
    assert result["variants"]
    assert all(v["seat_verified"] is True for v in result["variants"])


def test_seat_check_counts_unverified(db_session: Session):
    _seed(db_session)
    result = recommendation_tool(db_session, _profile(passengers=6))
    check = result["seat_check"]
    assert check is not None
    assert check["unverified"] == 1
    assert check["total"] == 2
    assert check["verified"] == 1
    assert check["required"] == 6


def test_seat_check_denominator_is_shown_cards_not_all_candidates(db_session: Session):
    """审查 M2 实测：分母曾用全部候选（12），但只展示 5 张卡。

    于是顶部说「12 款候选里只有 5 款查到座位数，卡片已标『座位未核实』」，
    而那 5 张上**一个标记都没有**——用户去找一个不存在的标记。
    用户只看得见展示出来的卡片，所以覆盖率必须按展示口径算。
    """
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="B", source=source)
    # 10 款无座位事实 + 3 款有座位事实，按评分排序后无座位的落在展示区
    for i in range(10):
        s = make_series(db_session, brand, name=f"无座{i}", body_type="suv",
                        energy_types=("BEV",), source=source)
        y = make_year(db_session, s)
        make_variant(db_session, s, y, price_cny=f"{150000 + i}", energy_type="BEV",
                     facts=[], source=source)
    for i in range(3):
        s = make_series(db_session, brand, name=f"有座{i}", body_type="suv",
                        energy_types=("BEV",), source=source)
        y = make_year(db_session, s)
        make_variant(db_session, s, y, price_cny=f"{190000 + i}", energy_type="BEV",
                     facts=[("参数信息", "座位数(个)", "6", "个", None)], source=source)
    db_session.commit()

    result = recommendation_tool(db_session, _profile(passengers=6), limit=5)
    shown = result["variants"]
    check = result["seat_check"]
    assert check is not None
    assert check["total"] == len(shown), "分母必须等于实际展示的卡片数"
    assert check["unverified"] == sum(1 for v in shown if not v["seat_verified"])


def test_seat_check_none_when_no_shown_card_is_unverified(db_session: Session):
    """未核实项全被筛掉/未进展示区时不得报数——否则文案与屏幕又一次对不上。"""
    ids = _seed(db_session)
    # limit=1 时只展示 1 张；把它挑成有座位的那张，则展示区里没有未核实项
    result = recommendation_tool(db_session, _profile(passengers=6), limit=1)
    if all(v["seat_verified"] for v in result["variants"]):
        assert result["seat_check"] is None
    else:
        assert result["seat_check"]["total"] == 1
    assert ids  # 夹具确实种了两款


def test_seat_check_none_when_all_verified(db_session: Session):
    """全部校验过就别报了——一句「你全都查过了」对用户没有价值。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="B", source=source)
    s = make_series(db_session, brand, name="全有座位", body_type="suv",
                    energy_types=("BEV",), source=source)
    y = make_year(db_session, s)
    make_variant(db_session, s, y, price_cny="150000", energy_type="BEV",
                 facts=[("参数信息", "座位数(个)", "6", "个", None)], source=source)
    db_session.commit()
    assert recommendation_tool(db_session, _profile(passengers=6))["seat_check"] is None


def test_seat_check_none_when_user_never_mentioned_passengers(db_session: Session):
    _seed(db_session)
    assert recommendation_tool(db_session, _profile())["seat_check"] is None


# ── 顶部说明文案 ────────────────────────────────────────────────────────────
def test_coverage_note_carries_concrete_numbers():
    note = AgentEngine._seat_coverage_note(
        {"required": 6, "verified": 3, "unverified": 2, "total": 5}
    )
    assert "6" in note and "5" in note and "2" in note
    assert "座位未核实" in note, "必须告诉用户卡片上那个标记是什么"


def test_coverage_note_silent_when_nothing_unverified():
    assert AgentEngine._seat_coverage_note(None) == ""
    assert AgentEngine._seat_coverage_note(
        {"required": 6, "verified": 5, "unverified": 0, "total": 5}
    ) == ""


# ── 「以上」的各种写法必须一致（审查 L1）─────────────────────────────────────
def test_all_yishang_spellings_get_the_same_open_bound():
    """审查 L1 实测：原先后缀集只有「以上」，「5 人开以上」「5 人或以上」会漏掉 +1。

    同一语义、三种写法、两种结果——用户换个说法就被少要一个座位。
    """
    from app.catalog.series_constraints import parse_passengers

    assert parse_passengers("5人以上") == 6
    assert parse_passengers("5人开以上") == 6
    assert parse_passengers("5人或以上") == 6
    # 含端点的写法不该 +1
    assert parse_passengers("5人以内") == 5
    assert parse_passengers("5人以下") == 5
