# -*- coding: utf-8 -*-
"""说不清需求的用户：不该被反复问同一个问题，也不该一款车都拿不到（2026-10-23）

## 来源：生产**真实用户会话**（不是构造的探针）

2026-10-23 12:47，生产上有一段 5 轮真实对话（`agent_turn_logs` 里 session
`e4f8bf12…`，用户是真人）。逐轮记录：

| 轮 | 用户说 | 助手回答 | 问题 |
| --- | --- | --- | --- |
| 0 | 「15万预算有什么能选的」 | 追问卡（正文空） | 用户**明确要清单**，却一款车都没拿到 |
| 1 | 「都可以」（答用途） | 「还差一项：平时主要坐几个人」 | 这句还行（问的是另一项） |
| 2 | 「2人」 | 「已记住…**还差一项：主要用途是什么？**」 | ❗**又问用途**——用户第 1 轮已答「都可以」 |
| 3 | 「都可以」（第二次） | 「**没法凭预算直接列一份推荐清单**」 | ❗库里明明有 870+ 款在售、预算内能列 5 款 |
| 4 | 「suv」 | 「已记住…**还差一项：主要用途是什么？**」 | ❗**第三次问用途** |

## 三个根因（都已定位到具体代码）

1. **「都可以」被路由成 `chitchat`**（`1:not(has_car_intent or structured)`）
   ⇒ 绕开推荐链 ⇒ 用户拿不到车；而且 LLM 在回答里说「用途不限」却
   **不回写画像** ⇒ 下一轮确定性追问仍把用途当缺失 ⇒ **同一个问题问三遍**。
2. **「用户放弃某一项」没有落到结构化画像** ⇒ 放弃只活在回复措辞里。
3. **用户明确要清单时仍被追问卡挡住** ⇒ 答非所问。

## 本文件的判据

- 路由层：「对追问的回答」走推荐链，不走闲聊；没有在追问时**保持原样**（不回归）。
- 追问层：用户放弃过的维度**不再问**（同一个问题不许问第二遍）。
- 端到端：**第一轮就给车**；5 轮里「用途」**最多被问一次**；全程不出现闲聊回复。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.engine import next_clarification
from app.agent.routing import asks_for_candidates, decide_route, gives_up_on_profile
from app.agent.schemas import Budget, UserProfile
from tests.seed import make_brand, make_sales, make_series, make_source, make_variant, make_year


def _seed_agent_data(db_session: Session) -> dict[str, int]:
    """Agent 测试种子（与 tests/test_agent.py 同款：一款 15 万内的 SUV + 轿车）。"""
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    suv = make_series(
        db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source
    )
    sedan = make_series(
        db_session, brand, name="通勤家轿", body_type="sedan", energy_types=("BEV",), source=source
    )
    year_suv, year_sedan = make_year(db_session, suv), make_year(db_session, sedan)
    v_suv = make_variant(
        db_session, suv, year_suv, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[("座位数", "seats", "5", "座", None), ("尺寸", "length_mm", "4650", "mm", None)],
        source=source,
    )
    v_sedan = make_variant(
        db_session, sedan, year_sedan, config_version="标准版", energy_type="BEV", price_cny="99800",
        facts=[("座位数", "seats", "5", "座", None), ("尺寸", "length_mm", "4680", "mm", None)],
        source=source,
    )
    db_session.commit()
    return {"suv": v_suv.id, "sedan": v_sedan.id}


def _create_session(client: TestClient) -> str:
    resp = client.post("/api/v1/agent/sessions")
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _send(client: TestClient, session_id: str, message: str) -> dict:
    resp = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages", json={"message": message}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _asked_missing(payload: dict) -> list[str]:
    """这一轮问了哪些维度（结构字段，不看措辞）。"""
    if payload.get("need_clarification") and payload.get("clarification"):
        return list(payload["clarification"].get("missing") or [])
    if payload.get("followup"):
        return list(payload["followup"].get("missing") or [])
    return []


# ── 1. 路由层 ──────────────────────────────────────────────────────────────


def test_give_up_answer_goes_to_recommendation_not_chitchat(db_session: Session):
    """「都可以」是对**上一轮追问**的回答 ⇒ 走推荐链，不走闲聊。"""
    profile = UserProfile()
    profile.unknowns = ["usage"]
    decision = decide_route(
        "都可以", hints={}, structured=False, profile=profile, resolved=[], db=db_session
    )
    assert decision.intent == "recommendation", decision
    assert decision.matched_rule.startswith("1b:"), decision


def test_give_up_without_pending_clarification_stays_chitchat(db_session: Session):
    """没有在追问时，「都可以」仍是普通对话（**不回归**）。"""
    profile = UserProfile()
    decision = decide_route(
        "都可以", hints={}, structured=False, profile=profile, resolved=[], db=db_session
    )
    assert decision.intent == "chitchat", decision


def test_give_up_synonyms_are_recognised():
    for msg in ("都可以", "都行", "随便", "无所谓", "你看着办", "你推荐"):
        assert gives_up_on_profile(msg), msg


def test_asks_for_candidates_recognises_list_requests():
    for msg in ("15万预算有什么能选的", "有什么车推荐", "有哪些能选", "帮我选几款", "选哪些车"):
        assert asks_for_candidates(msg), msg
    # 不是要清单的问法
    for msg in ("今天天气不错", "帮我算个题", "还有别的优惠吗", "续航怎么样"):
        assert not asks_for_candidates(msg), msg


# ── 2. 追问层：放弃过的维度不再问 ──────────────────────────────────────────


def test_waived_dimension_is_not_asked_again():
    """用户说了「都可以」（放弃用途）⇒ 之后不再问用途。"""
    profile = UserProfile()
    profile.budget = Budget(max=150000)
    assert next_clarification(profile).missing == ["usage"]

    profile.waived_dims = ["usage"]
    # 用途已被放弃 ⇒ 下一个该问的是人数
    assert next_clarification(profile).missing == ["passengers"]

    profile.passengers = 2
    assert next_clarification(profile) is None


def test_waived_passengers_is_not_asked_again():
    profile = UserProfile()
    profile.budget = Budget(max=150000)
    profile.usage = ["通勤"]
    profile.waived_dims = ["passengers"]
    assert next_clarification(profile) is None


# ── 3. 端到端：复刻那段真实对话 ────────────────────────────────────────────


def test_first_turn_gives_cars_instead_of_only_a_question(client: TestClient, db_session: Session):
    """「15万预算有什么能选的」⇒ **第一轮就给车**，缺口用尾巴问。"""
    _seed_agent_data(db_session)
    sid = _create_session(client)

    first = _send(client, sid, "15万预算有什么能选的")

    assert first["need_clarification"] is False, first
    assert first["recommended_variants"], first
    # 缺口**不阻塞**：改用 followup 尾巴问
    assert first.get("followup"), first
    assert first["followup"]["missing"] == ["usage"], first["followup"]
    # 预算硬门槛生效
    assert all(v["price_cny"] <= 150000 for v in first["recommended_variants"])


def test_give_up_turn_still_recommends(client: TestClient, db_session: Session):
    """用户回「都可以」⇒ **给车**，不是闲聊（此前是 LLM 自由发挥，一款车都没有）。"""
    _seed_agent_data(db_session)
    sid = _create_session(client)
    _send(client, sid, "15万预算有什么能选的")

    second = _send(client, sid, "都可以")

    assert second["need_clarification"] is False, second
    assert second["recommended_variants"], second


def test_waived_usage_is_never_asked_twice(client: TestClient, db_session: Session):
    """5 轮真实对话复刻：**用途最多被问一次**，其余各轮都在给车。"""
    _seed_agent_data(db_session)
    sid = _create_session(client)

    script = [
        "15万预算有什么能选的",  # 用户明确要清单
        "都可以",                  # 放弃用途
        "2人",                    # 补人数
        "都可以",                  # 再答一次
        "suv",                    # 补车身
    ]
    asked_usage = 0
    for i, msg in enumerate(script):
        payload = _send(client, sid, msg)
        assert payload["need_clarification"] is False, (i, msg, payload)
        assert payload["recommended_variants"], (i, msg, payload)
        if "usage" in _asked_missing(payload):
            asked_usage += 1

    assert asked_usage <= 1, f"用途被问了 {asked_usage} 次（生产实测里是 3 次）"


def test_body_type_applied_after_vague_answers(client: TestClient, db_session: Session):
    """用户后补的「suv」必须**生效**（放弃用途 ≠ 放弃车身）。"""
    ids = _seed_agent_data(db_session)
    sid = _create_session(client)
    _send(client, sid, "15万预算有什么能选的")
    _send(client, sid, "都可以")
    _send(client, sid, "2人")

    last = _send(client, sid, "suv")

    assert last["need_clarification"] is False, last
    got = [v["variant_id"] for v in last["recommended_variants"]]
    assert ids["suv"] in got, last
    assert ids["sedan"] not in got, last


# ── 4. 解释提示词：画像不全时不许模型替用户编 ──────────────────────────────


def test_explain_prompt_tells_model_not_to_assume_missing_dims():
    """画像缺项 ⇒ 明确告诉模型「还没说」，免得它替用户编一个用途。

    这是本次改动**带出来的风险**：画像不全也进推荐链 ⇒ 模型会看到 `usage: []`
    ⇒ 可能写「适合家庭出行」——用户从没说过的偏好被当成他说过的。
    """
    from app.agent.engine import _explain_system_prompt

    profile = UserProfile()
    profile.budget = Budget(max=150000)
    prompt = _explain_system_prompt(profile)
    assert "还没说" in prompt, prompt
    assert "用途" in prompt, prompt
    assert "不要替他假设" in prompt, prompt


def test_explain_prompt_unchanged_when_profile_complete():
    """画像完整 ⇒ 提示**一个字都不多**（既有行为逐位不变）。"""
    from app.agent.engine import _explain_system_prompt

    profile = UserProfile()
    profile.budget = Budget(max=150000)
    profile.usage = ["通勤"]
    profile.passengers = 2
    profile.body_type = ["suv"]
    prompt = _explain_system_prompt(profile)
    assert "还没说" not in prompt, prompt
    assert "不要替他假设" not in prompt, prompt


def test_explain_prompt_marks_waived_dim_as_unlimited_not_missing():
    """用户放弃的维度 ⇒ 说的是「**不限**」，不是「还没说」。"""
    from app.agent.engine import _explain_system_prompt

    profile = UserProfile()
    profile.budget = Budget(max=150000)
    profile.passengers = 2
    profile.waived_dims = ["usage"]
    prompt = _explain_system_prompt(profile)
    assert "不限" in prompt, prompt
    assert "不要替他猜" in prompt, prompt


# ── 5. 说不清需求 ⇒ 按销量排（用户建议：推荐销量高的车型）────────────────────


def _seed_sales_pair(db_session: Session) -> dict[str, int]:
    """两台车：**销量**与**软评分**故意相反——用来钉住排序口径。

    - 「销量小车」：车短、功率低，但**卖得多**；
    - 「参数大车」：车长、功率高，但**卖得少**。

    ⇒ 按软评分排 → 参数大车在前；按销量排 → 销量小车在前。
    """
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    small = make_series(
        db_session, brand, name="销量小车", body_type="sedan", energy_types=("BEV",), source=source
    )
    big = make_series(
        db_session, brand, name="参数大车", body_type="suv", energy_types=("BEV",), source=source
    )
    year_small, year_big = make_year(db_session, small), make_year(db_session, big)
    v_small = make_variant(
        db_session, small, year_small, config_version="标准版", energy_type="BEV", price_cny="120000",
        facts=[("尺寸", "length_mm", "4200", "mm", None), ("动力", "power_kw", "100", "kW", None)],
        source=source,
    )
    v_big = make_variant(
        db_session, big, year_big, config_version="标准版", energy_type="BEV", price_cny="140000",
        facts=[("尺寸", "length_mm", "5000", "mm", None), ("动力", "power_kw", "300", "kW", None)],
        source=source,
    )
    make_sales(db_session, small, "2026-09", 12000, source=source)
    make_sales(db_session, big, "2026-09", 800, source=source)
    db_session.commit()
    return {"sales_car": v_small.id, "spec_car": v_big.id}


def test_vague_user_gets_best_sellers_first(db_session: Session):
    """用户**说不清需求**（只有预算）⇒ 按**销量**排。

    2026-10-23 生产真实用户会话：用户问「15万预算有什么能选的」，
    画像里只有预算 ⇒ 软评分 8 个维度几乎全是中性分 ⇒ 排序实际由
    「车长/功率」决定 ⇒ 给用户的不是**热销的**车，而是**参数唬人的**车。
    用户建议：「推荐销量高的车型」。
    """
    from app.agent.tools import recommendation_tool

    ids = _seed_sales_pair(db_session)
    profile = UserProfile()
    profile.budget = Budget(max=150000)

    result = recommendation_tool(db_session, profile)

    got = [v["variant_id"] for v in result["variants"]]
    assert got, result
    assert got[0] == ids["sales_car"], got
    # 销量数字随候选带上 ⇒ 解释里可以**引用**它（可溯源，不是模型编的）
    assert result["variants"][0]["sales_count"] == 12000, result["variants"][0]


def test_order_unchanged_when_user_has_soft_info(db_session: Session):
    """画像里有软线索（用途=家庭）⇒ 仍按**软评分**排，**不带销量**。

    ⇒ 既有推荐结果**逐位不变**（这是本次改动最该守住的一条）。
    """
    from app.agent.tools import recommendation_tool

    ids = _seed_sales_pair(db_session)
    profile = UserProfile()
    profile.budget = Budget(max=150000)
    profile.usage = ["家庭"]

    result = recommendation_tool(db_session, profile)

    got = [v["variant_id"] for v in result["variants"]]
    assert got[0] == ids["spec_car"], got
    assert "sales_count" not in result["variants"][0], result["variants"][0]


def test_sales_order_falls_back_to_score_without_sales_data(db_session: Session):
    """库里没有销量 ⇒ 仍按软评分排（**不能因为没数据就不给车**）。"""
    from app.agent.tools import recommendation_tool
    from tests.seed import make_brand, make_series, make_source, make_variant, make_year

    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="无销量品牌", source=source)
    series = make_series(
        db_session, brand, name="无销量车系", body_type="sedan", energy_types=("BEV",), source=source
    )
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV", price_cny="120000",
        facts=[("尺寸", "length_mm", "4200", "mm", None)], source=source,
    )
    db_session.commit()

    profile = UserProfile()
    profile.budget = Budget(max=150000)
    result = recommendation_tool(db_session, profile)

    assert result["variants"], result
    assert "sales_count" not in result["variants"][0], result["variants"][0]


def test_explain_prompt_says_batch_is_ordered_by_sales_for_vague_user():
    """模型必须知道这批是**按销量**排的——否则它会写成「按你的需求匹配」（假的）。

    断言用**整句**而不是「销量」两个字：基础提示里本来就有「禁止编造价格、销量…」
    ⇒ 断言「销量」在提示里永远成立，等于没断言。
    """
    from app.agent.engine import _explain_system_prompt

    profile = UserProfile()
    profile.budget = Budget(max=150000)
    prompt = _explain_system_prompt(profile)
    assert "按最近一个月销量" in prompt, prompt

    # 画像有软线索时不该出现这句（那批不是按销量排的）
    with_info = UserProfile()
    with_info.budget = Budget(max=150000)
    with_info.usage = ["通勤"]
    assert "按最近一个月销量" not in _explain_system_prompt(with_info)
