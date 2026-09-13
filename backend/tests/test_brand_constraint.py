"""品牌硬约束与品牌盘点（2026-09 用户实测缺陷的回归测试）。

背景：用户第一句就说「必须是奔驰」，但画像里没有品牌字段 → 收齐预算/用途/人数后
推荐了领克/小鹏/大众/林肯；追问「没有燃油的吗」又推了其他品牌；「奔驰还有哪些车型」
由模型凭记忆答成「3 款、都是纯电」（库里实际 57 款、燃油 37 款）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.engine import (
    UserProfile,
    asks_brand_lineup,
    energy_asked_in,
    merge_profile,
)
from app.agent.tools import TOOL_SCHEMAS, recommendation_tool, vehicle_search
from app.catalog.brands import brand_series_overview, resolve_brand_mentions
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _benz_vs_others(db: Session) -> dict:
    """奔驰（含 ICE 与 BEV）+ 两个其他品牌，价位都落在 20~30 万。"""
    source = make_source(db)
    benz = make_brand(db, name="奔驰", brand_type="luxury", source=source)
    lixiang = make_brand(db, name="理想", brand_type="domestic_nev", source=source)
    byd = make_brand(db, name="比亚迪", brand_type="domestic_nev", source=source)
    ids: dict[str, int] = {"benz": benz.id}
    for brand, name, energy, price in (
        (benz, "奔驰A级", "ICE", "260000"),
        (benz, "奔驰EQE", "BEV", "480000"),
        (lixiang, "理想L6", "EREV", "250000"),
        (byd, "汉L", "PHEV", "259800"),
    ):
        series = make_series(db, brand, name=name, energy_types=(energy,), source=source)
        year = make_year(db, series)
        make_variant(db, series, year, config_version="标准版", energy_type=energy,
                     price_cny=price, source=source)
    db.commit()
    return ids


def test_resolve_brand_mentions_positive_and_negative(db_session: Session):
    _benz_vs_others(db_session)
    assert resolve_brand_mentions(db_session, "必须是奔驰")["brand_ids"]  # 正向
    assert resolve_brand_mentions(db_session, "只要奔驰")["brand_labels"] == ["奔驰"]
    # 否定 → 排除项；且不再进正向集合
    neg = resolve_brand_mentions(db_session, "不要奔驰")
    assert "brand_ids" not in neg and neg["brand_exclude_ids"]
    # 未提及品牌 → 不写入画像
    assert resolve_brand_mentions(db_session, "预算 20 万，家用") == {}


def test_ambiguous_brand_requires_intent(db_session: Session):
    """「理想」既是品牌也是日常词：无购车意图词时不得当作品牌约束。"""
    _benz_vs_others(db_session)
    assert resolve_brand_mentions(db_session, "理想的预算大概是 20 万") == {}
    assert resolve_brand_mentions(db_session, "我想买理想")["brand_labels"] == ["理想"]


def test_merge_profile_accumulates_and_excludes_brands():
    profile = merge_profile(UserProfile(), {"brand_ids": [1], "brand_labels": ["奔驰"]})
    assert profile.brand_ids == [1] and profile.brand_labels == ["奔驰"]
    # 会话内再次提及另一品牌 → 并集（不覆盖）
    profile = merge_profile(profile, {"brand_ids": [2], "brand_labels": ["宝马"]})
    assert profile.brand_ids == [1, 2]
    # 之后明确排除其中一个 → 从正向集合移除，进排除集合
    profile = merge_profile(profile, {"brand_exclude_ids": [1]})
    assert profile.brand_ids == [2] and profile.brand_exclude_ids == [1]


def test_recommendation_respects_brand_constraint(db_session: Session):
    """核心回归：声明品牌后，推荐结果不得出现其他品牌。"""
    _benz_vs_others(db_session)
    profile = UserProfile()
    profile.budget.max = 300000
    profile.brand_ids = [
        b for b in resolve_brand_mentions(db_session, "只要奔驰")["brand_ids"]
    ]
    result = recommendation_tool(db_session, profile)
    assert result["variants"], "奔驰在预算内有在售款型，不应为空"
    assert {v["brand_name"] for v in result["variants"]} == {"奔驰"}

    # 反向：排除奔驰后不得再出现奔驰
    profile.brand_ids = []
    profile.brand_exclude_ids = [
        b for b in resolve_brand_mentions(db_session, "不要奔驰")["brand_exclude_ids"]
    ]
    excluded_result = recommendation_tool(db_session, profile)
    assert "奔驰" not in {v["brand_name"] for v in excluded_result["variants"]}


def test_brand_overview_counts_from_db(db_session: Session):
    """品牌盘点：数量与能源构成来自库内事实，而不是模型记忆。"""
    _benz_vs_others(db_session)
    ids = resolve_brand_mentions(db_session, "奔驰")["brand_ids"]
    overview = brand_series_overview(db_session, ids)
    assert overview["series_count"] == 2
    assert overview["fuel_series_count"] == 1        # 奔驰A级（ICE）
    assert overview["new_energy_series_count"] == 1   # 奔驰EQE（BEV）
    names = {i["series_name"] for i in overview["series"]}
    assert names == {"奔驰A级", "奔驰EQE"}


def test_brand_lineup_intents():
    assert asks_brand_lineup("奔驰都有哪些车型")
    assert asks_brand_lineup("没有燃油的吗")
    assert asks_brand_lineup("有纯电的吗")
    assert not asks_brand_lineup("预算 20 万，家用 5 人")
    assert energy_asked_in("没有燃油的吗") == ["fuel"]
    assert energy_asked_in("有纯电的吗") == ["BEV"]
    assert energy_asked_in("都有哪些车型") is None


def test_vehicle_search_by_brand_name(db_session: Session):
    """工具按品牌名查询（模型拿不到 brand_id，此前无法列全品牌车型）。"""
    _benz_vs_others(db_session)
    benz = vehicle_search(db_session, brand="奔驰", limit=50)
    assert {s["series_name"] for s in benz} == {"奔驰A级", "奔驰EQE"}
    assert all(s["brand_name"] == "奔驰" for s in benz)
    # 库里没有的品牌名：如实返回空，不猜
    assert vehicle_search(db_session, brand="不存在的品牌XYZ") == []
    # 能源过滤叠加
    assert [s["series_name"] for s in vehicle_search(db_session, brand="奔驰", energy_type="fuel")] == ["奔驰A级"]


def test_vehicle_search_schema_exposes_brand():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "vehicle_search")
    props = schema["function"]["parameters"]["properties"]
    assert "brand" in props, "必须让模型能按品牌名查询，否则只能凭记忆列举"
    assert "limit" in props


def test_zero_result_message_mentions_brand(db_session: Session):
    """品牌 + 预算无匹配时，文案要带上品牌（说明约束被采纳），且不得引导看其他品牌。"""
    _benz_vs_others(db_session)
    from app.agent.engine import AgentEngine

    profile = UserProfile()
    profile.budget.max = 100000  # 奔驰最低 26 万 → 必然为空
    profile.brand_ids = resolve_brand_mentions(db_session, "只要奔驰")["brand_ids"]
    profile.brand_labels = ["奔驰"]
    result = recommendation_tool(db_session, profile)
    assert result["variants"] == []
    text = AgentEngine._template_explanation(profile, result, [])
    assert "奔驰" in text and "10 万元" in text and "其他品牌" not in text


def test_reasserted_brand_returns_recommendations(client, db_session):
    """「我强调过了，只要奔驰」：只重申品牌、无购车意图词的消息也必须走确定性链路。

    实测缺陷：这类消息此前掉进通用对话分支（filters 为空），由模型凭记忆答
    「我这边现有的奔驰资料只有三款纯电车型」——而库里是 57 款、燃油 40 款。
    """
    _benz_vs_others(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    for message in ("我想买车，必须是奔驰", "20~30万", "上下班通勤", "1~2人"):
        client.post(f"/api/v1/agent/sessions/{sid}/messages", json={"message": message})
    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages", json={"message": "我强调过了，只要奔驰"}
    ).json()
    assert rec["filters"].get("brand_labels") == ["奔驰"]
    assert rec["recommended_variants"], "重申品牌约束后必须给出该品牌内的推荐"
    assert {v["brand_name"] for v in rec["recommended_variants"]} == {"奔驰"}


def test_series_name_containing_brand_is_not_a_brand_constraint(client, db_session):
    """「银河星愿怎么样」里的「银河」是车系名的一部分，不得当成「只要银河」的品牌约束。

    实测回归：一旦误判成品牌约束，下一轮「想要15万的燃油车」会按 银河+燃油 过滤而得到空结果。
    """
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="银河", source=source)
    series = make_series(db_session, brand, name="银河星愿", energy_types=("BEV",), source=source)
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="410km", energy_type="BEV",
                 price_cny="83800", source=source)
    db_session.commit()

    assert resolve_brand_mentions(db_session, "银河星愿怎么样",
                                 series_names=["银河星愿"]) == {}
    # 消息基本只有品牌名时仍然按约束处理（用户在回答「哪个品牌」）
    assert resolve_brand_mentions(db_session, "银河")["brand_labels"] == ["银河"]
