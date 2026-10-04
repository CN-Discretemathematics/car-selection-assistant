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
