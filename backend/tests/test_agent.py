"""Agent 接口与推荐管线集成测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import VehicleVariant
from app.rag.service import reset_index
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_agent_data(db_session: Session) -> dict[str, int]:
    """Agent 测试专用种子（只用于测试，见 tests/seed.py 说明）。"""
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    suv = make_series(db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    sedan = make_series(db_session, brand, name="通勤家轿", body_type="sedan", energy_types=("BEV",), source=source)
    ice = make_series(db_session, brand, name="燃油轿车", body_type="sedan", energy_types=("ICE",), source=source)
    year_suv, year_sedan, year_ice = make_year(db_session, suv), make_year(db_session, sedan), make_year(db_session, ice)

    v_suv = make_variant(
        db_session, suv, year_suv, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[
            ("座位数", "seats", "5", "座", None),
            ("尺寸", "length_mm", "4650", "mm", None),
            ("动力", "power_kw", "150", "kW", None),
            ("舒适性", "leather_seats", "仿皮座椅", None, None),
        ],
        source=source,
    )
    v_sedan = make_variant(
        db_session, sedan, year_sedan, config_version="标准版", energy_type="BEV", price_cny="99800",
        facts=[
            ("座位数", "seats", "5", "座", None),
            ("尺寸", "length_mm", "4680", "mm", None),
            ("动力", "power_kw", "120", "kW", None),
        ],
        source=source,
    )
    v_ice = make_variant(
        db_session, ice, year_ice, config_version="豪华版", energy_type="ICE", price_cny="179800",
        facts=[("座位数", "seats", "5", "座", None), ("动力", "power_kw", "137", "kW", None)],
        source=source,
    )
    db_session.commit()
    return {"suv": v_suv.id, "sedan": v_sedan.id, "ice": v_ice.id}


def _create_session(client: TestClient) -> str:
    resp = client.post("/api/v1/agent/sessions")
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _send(client: TestClient, session_id: str, message: str) -> dict:
    resp = client.post(f"/api/v1/agent/sessions/{session_id}/messages", json={"message": message})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_agent_clarification_flow(client: TestClient, db_session: Session):
    ids = _seed_agent_data(db_session)
    session_id = _create_session(client)

    first = _send(client, session_id, "我想买台车")
    assert first["need_clarification"] is True
    assert "预算" in first["clarification"]["question"]

    second = _send(client, session_id, "预算12万")
    assert second["need_clarification"] is True
    assert "用途" in second["clarification"]["question"]

    third = _send(client, session_id, "平时上下班通勤，两个人")
    assert third["need_clarification"] is False
    # 预算 ≤12万 → 通勤家轿（9.98万）入选；家用SUV（12.98万）与燃油轿车（17.98万）被硬约束排除
    got = [v["variant_id"] for v in third["recommended_variants"]]
    assert ids["sedan"] in got
    assert ids["suv"] not in got
    assert ids["ice"] not in got
    assert all(v["price_cny"] <= 120000 for v in third["recommended_variants"])
    assert "通勤家轿" in (third["explanation"] or "")


def test_agent_recommendation_with_sources(client: TestClient, db_session: Session):
    reset_index()
    _seed_agent_data(db_session)
    session_id = _create_session(client)

    body = _send(client, session_id, "预算15万，家庭用车，5口人，想要新能源SUV")
    assert body["need_clarification"] is False
    variants = body["recommended_variants"]
    assert variants, "应有推荐结果"

    # 硬约束：预算 ≤15万、新能源、SUV
    assert all(v["price_cny"] <= 150000 for v in variants)
    assert all(v["energy_type"] in ("BEV", "PHEV", "EREV") for v in variants)
    assert all(v["series_name"] == "家用SUV" for v in variants)
    # 事实可追溯：每个推荐都有来源引用，且推荐 id 真实存在于数据库
    assert body["citations"], "必须有来源引用"
    for v in variants:
        assert db_session.get(VehicleVariant, v["variant_id"]) is not None
    assert body["official_links"], "必须返回官方车型页链接"
    assert body["filters"]["budget_max"] == 150000
    # §13 混合检索证据：官方资料片段进入解释佐证
    assert "官方资料佐证" in (body["explanation"] or "")


def test_agent_no_match_explains(client: TestClient, db_session: Session):
    _seed_agent_data(db_session)
    session_id = _create_session(client)

    body = _send(client, session_id, "预算5万，家庭用车，5口人，想要燃油MPV")
    assert body["need_clarification"] is False
    assert body["recommended_variants"] == []
    assert "建议放宽" in (body["explanation"] or "")


def test_agent_stream_replays_result(client: TestClient, db_session: Session):
    _seed_agent_data(db_session)
    session_id = _create_session(client)
    _send(client, session_id, "预算15万，家庭用车，5口人，想要新能源SUV")

    resp = client.get(f"/api/v1/agent/sessions/{session_id}/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: message" in resp.text
    assert "event: done" in resp.text


def test_agent_session_404(client: TestClient, db_session: Session):
    _seed_agent_data(db_session)
    assert client.post("/api/v1/agent/sessions/unknown/messages", json={"message": "hi"}).status_code == 404
    assert client.get("/api/v1/agent/sessions/unknown/stream").status_code == 404


def test_agent_smalltalk_replies_naturally(client: TestClient, db_session: Session):
    """问候/能力咨询走自然回复，不再死板追问预算（真实智能客服体验）。"""
    _seed_agent_data(db_session)
    session_id = _create_session(client)

    out = _send(client, session_id, "你好")
    assert out["need_clarification"] is False
    assert out["clarification"] is None
    assert out["explanation"], "问候应给出自然文字回复"
    assert out["recommended_variants"] == []

    out = _send(client, session_id, "你能帮我什么")
    assert out["need_clarification"] is False
    assert out["explanation"]

    # 含购车内容的句子不受闲聊分支影响，仍进入推荐/追问链路
    out = _send(client, session_id, "你好，我想买台车")
    assert out["need_clarification"] is True


def test_agent_general_advice_answers_without_profile(client: TestClient, db_session: Session):
    """通用购车咨询（无画像）应给出有据可查的回答，而不是拍脑袋推荐。"""
    _seed_agent_data(db_session)
    session_id = _create_session(client)
    out = _send(client, session_id, "电动车和油车哪个好？")
    assert out["need_clarification"] is False
    assert out["explanation"]
    assert out["recommended_variants"] == []


def test_agent_clarify_twice_then_guides_gently(client: TestClient, db_session: Session):
    """追问两次仍无信息：温和引导，不硬推「没有预算的推荐」。"""
    _seed_agent_data(db_session)
    session_id = _create_session(client)

    assert _send(client, session_id, "我想买台车")["need_clarification"] is True
    out = _send(client, session_id, "我还没想好预算呢")
    assert out["need_clarification"] is False
    assert out["recommended_variants"] == []
    assert out["explanation"], "应给出温和引导而不是推荐列表"

    # 已有预算后授权宽松：进入下一步追问，补齐用途后正常推荐
    out = _send(client, session_id, "预算15万，随便推荐吧")
    assert out["need_clarification"] is True  # 补充追问用途，不再空画像硬推
    out = _send(client, session_id, "家庭用，5口人")
    assert out["need_clarification"] is False
    assert out["recommended_variants"], "画像齐全 + 授权宽松后应正常推荐"


def test_agent_give_up_requires_core_profile(client: TestClient, db_session: Session):
    """仅能源偏好不足以授权宽松推荐（评审 S1）：无预算/用途/人数时给引导而非硬推。"""
    _seed_agent_data(db_session)
    session_id = _create_session(client)
    assert _send(client, session_id, "想要纯电的车")["need_clarification"] is True
    out = _send(client, session_id, "随便推荐吧")
    assert out["need_clarification"] is False
    assert out["recommended_variants"] == []
    assert out["explanation"], "应温和引导而不是出推荐列表"


def test_agent_avoid_hard_filter(client: TestClient, db_session: Session):
    """用户「不要燃油车」时，燃油候选必须被硬排除（评审 L2）。"""
    _seed_agent_data(db_session)
    session_id = _create_session(client)
    out = _send(client, session_id, "不要燃油车，预算18万，家庭用5人")
    assert out["need_clarification"] is False
    recs = out["recommended_variants"]
    assert recs, "预算内应存在 BEV 候选"
    assert all(r["energy_type"] != "ICE" for r in recs), "avoid 应硬过滤燃油车"


def test_extract_hints_plugin_hybrid_not_hev():
    """「插电混动」只命中 PHEV，不得双重命中 HEV（评审 M3）。"""
    from app.agent.engine import extract_hints

    assert extract_hints("想要插电混动的SUV")["energy_preference"] == ["PHEV"]
    assert extract_hints("插电式混动轿车")["energy_preference"] == ["PHEV"]
    # 用户同时明确提油混时两者都保留
    hints = extract_hints("插电混动和油混都想看看")
    assert "PHEV" in hints["energy_preference"] and "HEV" in hints["energy_preference"]


def test_extract_hints_budget_range_and_loose():
    """评审 P1：双侧带万区间、多万/出头 的预算解析。"""
    from app.agent.engine import extract_hints

    assert extract_hints("预算20万到30万之间")["budget"] == {"min": 200000, "max": 300000}
    assert extract_hints("20-30万")["budget"] == {"min": 200000, "max": 300000}
    assert extract_hints("20万到30万")["budget"] == {"min": 200000, "max": 300000}
    assert extract_hints("20多万")["budget"] == {"min": 200000}
    assert extract_hints("30万出头")["budget"] == {"min": 300000}
    assert extract_hints("预算15万")["budget"] == {"max": 150000}


def test_extract_hints_usage_synonyms():
    """「日常/代步」→ 通勤，「接送/买菜」→ 家庭（此前「日常用」漏识别导致乱回）。"""
    from app.agent.engine import extract_hints

    assert extract_hints("预算8万，日常用，2人") == {
        "budget": {"max": 80000},
        "usage": ["通勤"],
        "passengers": 2,
    }
    assert extract_hints("每天上下班代步")["usage"] == ["通勤"]
    assert extract_hints("接送孩子买菜")["usage"] == ["家庭"]


def test_agent_usage_synonyms_complete_profile(client: TestClient, db_session: Session):
    """「预算X万，日常用，2人」= 完整核心画像 → 直接推荐，不再「慢慢想就好」（生产故障回归）。"""
    _seed_agent_data(db_session)
    session_id = _create_session(client)
    assert _send(client, session_id, "帮我推荐一台车")["need_clarification"] is True
    _send(client, session_id, "预算12万")  # 追问用途 → unknowns=[usage]
    body = _send(client, session_id, "预算10万，日常用，2人")
    assert body["need_clarification"] is False
    assert body["recommended_variants"], "预算+日常+2人 画像齐全应直接给出推荐"
    assert "没关系" not in (body["explanation"] or "")


def test_agent_partial_info_targeted_guidance(client: TestClient, db_session: Session):
    """缺一项但本轮给了可解析信息（预算+人数）：复述已收集内容并只问用途，而非「慢慢想就好」。"""
    _seed_agent_data(db_session)
    session_id = _create_session(client)
    assert _send(client, session_id, "想买台车")["need_clarification"] is True
    assert _send(client, session_id, "预算12万")["need_clarification"] is True  # 追问用途
    body = _send(client, session_id, "预算12万，2个人")
    assert body["need_clarification"] is False
    text = body["explanation"] or ""
    assert "已记住" in text and "预算不超过 12 万元" in text and "2 人乘坐" in text
    assert "主要用途" in text
    assert "没关系" not in text


def test_chat_evidence_bridges_locked_series(db_session: Session):
    """评审 M-R9：指代性消息（「我看他们价格差不多」）检索回退到会话内锁定的车系。

    回归场景：上轮 App 报过参数/价格，本轮用户说「我看他们价格差不多」——
    当前消息解析不出车系，若不借用会话锁定车系，LLM 的证据为空，
    会按「没有数据就如实说不知道」自我否定上一轮数字。
    """
    from app.agent.engine import _retrieve_chat_evidence
    from app.catalog.series_index import resolve_series

    _seed_agent_data(db_session)
    reset_index()
    suv_sid = resolve_series(db_session, "家用SUV")[0][0].id

    evidence = _retrieve_chat_evidence(db_session, "我看他们价格差不多", None, [suv_sid])
    assert evidence, "会话锁定车系应兜底检索到证据"
    assert {e["series_id"] for e in evidence} == {suv_sid}, "证据应全部来自锁定的车系"

    # 无锁定的旧行为：纯消息检索（可以不命中，但不得抛错）
    assert isinstance(_retrieve_chat_evidence(db_session, "我看他们价格差不多", None, None), list)


def test_extract_hints_weight_emphasis():
    """§17.2 落地：强调句式（X 优先/看重/主要看）→ 对应评分维度权重线索。"""
    from app.agent.engine import extract_hints

    hints = extract_hints("空间优先，动力也比较看重")
    assert hints["weights"] == {"space": 0.2, "power": 0.2}
    assert extract_hints("最看重续航")["weights"] == {"energy": 0.2}
    assert extract_hints("主要看性价比")["weights"] == {"budget": 0.2}
    # 无强调词不产生权重线索（不污染画像与闲聊判定）
    assert "weights" not in extract_hints("预算12万，2个人，想要纯电SUV")
    assert "weights" not in extract_hints("今天天气不错")


def test_merge_profile_accumulates_weights():
    """跨轮次权重累加：第一轮「空间优先」，后续「更看重续航」→ 两维都加成。"""
    from app.agent.engine import extract_hints, merge_profile
    from app.agent.schemas import UserProfile

    profile = merge_profile(UserProfile(), extract_hints("空间优先"))
    profile = merge_profile(profile, extract_hints("更看重续航"))
    assert profile.weights == {"space": 0.2, "energy": 0.2}
