# -*- coding: utf-8 -*-
"""回答契约自证测试（P2）：数字/名字必须 ⊆ 本轮工具返回，错误在发送前被拦截。

四条 FakeLLM 路径（LLM 全部用脚本假件，不触真实 API）：
1. 编造数字 → 被拦（回灌违规项重写）；
2. 第一次违规、重写合格 → 放行且 LLM 恰好被调两次；
3. 两次违规 → 回退确定性回复并在 filters["contract_fallback"]=True 打标；
4. 合规答案 → 零额外 LLM 调用。
外加 numbers_in / names_in / validate_tool_answer / tool_result_universe /
check_catalog_overview_text 的单元断言与 answer_numbers_allowed 的迁移兼容性。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.answer_contract import (
    answer_numbers_allowed,
    check_catalog_overview_text,
    names_in,
    numbers_in,
    tool_result_universe,
    validate_tool_answer,
)
from app.agent.engine import get_agent_engine
from tests.seed import make_brand, make_series, make_source, make_variant, make_year
from tests.test_tool_loop import _FakeLLM, _client_with_llm


def _seed(db: Session):
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="丰田", source=source)
    series = make_series(db, brand, name="凯美瑞", body_type="sedan",
                         energy_types=("ICE",), source=source)
    year = make_year(db, series)
    make_variant(db, series, year, config_version="2.0G", energy_type="ICE",
                 price_cny="200000", source=source)
    db.commit()


def _tool_call(name: str = "vehicle_search", **arguments) -> dict:
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


# ── 单元断言 ────────────────────────────────────────────────────────────────

def test_numbers_in_extracts_all_decimals():
    assert numbers_in("官方指导价 20 万元，续航 510km，油耗 4.56L") == {20.0, 510.0, 4.56}
    assert numbers_in("没有任何数字") == set()
    assert numbers_in("") == set()


def test_names_in_shape_candidates_and_generic_filter():
    # 「CJK 前缀 + 拉丁核心」形状候选；连接词不吞进候选
    assert names_in("奔驰EQE和腾势Z9GT") == {"奔驰EQE", "腾势Z9GT"}
    # 纯 CJK 车名不做形状猜测（宁漏勿错）
    assert names_in("凯美瑞和汉兰达都不错") == set()
    # 通用缩写按拉丁核心过滤；核心为版本词的车名（秦PLUS）不做名字判定（宁漏勿错）
    assert names_in("这台SUV支持CLTC续航700km") == set()
    assert names_in("秦PLUS在售") == set()
    assert "A" not in names_in("A级")


def test_names_in_with_vocabulary_hits():
    vocab = {"凯美瑞", "丰田"}
    assert names_in("丰田凯美瑞在售", vocab) == {"凯美瑞", "丰田"}
    assert names_in("只提凯美瑞", vocab) == {"凯美瑞"}
    assert names_in("都没有", vocab) == set()


def test_validate_tool_answer_numbers_and_names():
    # 编造数字 → 违规描述带具体数字
    violations = validate_tool_answer("卖 99 万元", {20.0}, set())
    assert len(violations) == 1 and "99" in violations[0]
    # 合法数字（0.01 容差）+ 词表名字 → 合格
    assert validate_tool_answer("20.0001 万元，秦PLUS在售", {20.0}, {"秦PLUS"}) == []
    # 名字容差：候选与允许名互为子串即同源（品牌前缀拼接）
    assert validate_tool_answer("丰田凯美瑞在售", {20.0}, {"凯美瑞"}) == []
    # 编造拉丁形状车名 → 名字违规（描述里带具体候选名）
    violations = validate_tool_answer("奔驰EQE在售", {20.0}, {"凯美瑞"})
    assert len(violations) == 1 and "奔驰EQE" in violations[0]
    # 编造带数字的车名 → 名字违规 + 车名内数字双重触发（belt and suspenders）
    violations = validate_tool_answer("腾势Z9GT在售", {20.0}, {"凯美瑞"})
    assert any("腾势Z9GT" in v for v in violations)


def test_tool_result_universe_numbers_with_wan_derivative():
    result = {"results": [{
        "series_id": 1, "series_name": "凯美瑞", "brand_name": "丰田",
        "price_range": {"min": 200000.0, "max": 200000.0}, "source_id": 1,
    }]}
    numbers, names = tool_result_universe(result)
    assert 200000.0 in numbers and 20.0 in numbers  # /10000 万元派生口径
    assert {"凯美瑞", "丰田"} <= names


def test_answer_numbers_allowed_reexport_from_engine():
    """迁移兼容：engine 侧 re-export 保持既有调用点（对比分析链）可用。"""
    from app.agent.engine import answer_numbers_allowed as via_engine

    assert via_engine is answer_numbers_allowed
    ok, reason = answer_numbers_allowed("续航 200km", {20000.0})
    assert not ok and "200" in reason  # 子串陷阱仍被数值集合+容差挡住
    assert answer_numbers_allowed("续航 20000", {20000.0}) == (True, None)


def test_check_catalog_overview_text_tripwire():
    overview = {"series_count": 3, "variant_count": 3, "brand_count": 2,
                "fuel_series_count": 2, "new_energy_series_count": 1}
    assert check_catalog_overview_text("共 3 个车系、3 个款型，覆盖 2 个品牌。", overview) == []
    problems = check_catalog_overview_text("共 9 个车系。", overview)
    assert len(problems) == 1 and "9" in problems[0]


def test_log_route_decision_outputs_single_line_json(caplog):
    from app.agent.routing import RouteDecision, log_route_decision

    with caplog.at_level("INFO", logger="app.agent.routing"):
        log_route_decision(
            "session-secret-1234", "全部车型有多少款车？",
            RouteDecision("catalog_count", "0.7b:asks_catalog_count"), elapsed_ms=1.234,
        )
    line = caplog.records[-1].getMessage()
    payload = json.loads(line)  # 单行合法 JSON
    assert payload["intent"] == "catalog_count"
    assert payload["matched_rule"].startswith("0.7b")
    assert payload["elapsed_ms"] == 1.23
    assert payload["sid"] != "session-secret-1234" and len(payload["sid"]) == 12  # 匿名 id
    assert "ryan" not in line and "@" not in line and "ip" not in payload  # 不含身份/IP 字段


# ── FakeLLM 集成路径 ────────────────────────────────────────────────────────

def test_contract_blocks_fabricated_number_then_accepts_rewrite(client: TestClient, db_session: Session):
    """路径 1：编造数字被拦，违规项回灌重写后放行（LLM 共 3 次调用）。"""
    _seed(db_session)
    fake = _FakeLLM([
        _tool_call("vehicle_search", query="凯美瑞"),
        {"content": "凯美瑞官方指导价 99 万元，非常划算。"},   # 99 不在工具结果 → 违规
        {"content": "凯美瑞在售，官方指导价 20 万元。"},        # 重写合格（200000 的万元口径）
    ])
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下凯美瑞现在的情况，给点官方信息"}).json()
        assert out["explanation"] == "凯美瑞在售，官方指导价 20 万元。"
        assert out["filters"]["tool_loop"] is True
        assert "contract_fallback" not in out["filters"]
        assert len(fake.calls) == 3
        # 违规项必须回灌给模型（重写消息里包含违规描述）
        rewrite_prompt = fake.calls[-1][-1]["content"]
        assert "工具结果之外" in rewrite_prompt and "99" in rewrite_prompt
    finally:
        restore()


def test_contract_rewrite_after_first_violation_two_llm_calls(client: TestClient, db_session: Session):
    """路径 2：第一次违规、重写合格 → 放行且 LLM 恰好被调两次。"""
    _seed(db_session)
    fake = _FakeLLM([
        {"content": "凯美瑞卖 66 万元。"},                      # 未调工具即报数 → 违规
        {"content": "凯美瑞在售，具体价格以款型页为准。"},        # 重写无数字 → 合格
    ])
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下凯美瑞现在的情况，给点官方信息"}).json()
        assert out["explanation"] == "凯美瑞在售，具体价格以款型页为准。"
        assert out["filters"]["tool_loop"] is True
        assert "contract_fallback" not in out["filters"]
        assert len(fake.calls) == 2
    finally:
        restore()


def test_contract_double_violation_falls_back_deterministic(client: TestClient, db_session: Session):
    """路径 3：两次违规 → 回退既有确定性兜底，filters 打标 contract_fallback。"""
    _seed(db_session)
    fake = _FakeLLM([
        {"content": "凯美瑞卖 66 万元。"},
        {"content": "凯美瑞只要 55 万元，性价比很高。"},         # 重写仍编造数字
    ])
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下凯美瑞现在的情况，给点官方信息"}).json()
        assert out["filters"].get("tool_loop") is False
        assert out["filters"].get("contract_fallback") is True
        assert out["filters"].get("contract_violations", 0) >= 1
        assert out["filters"].get("tool_calls") == 0
        assert "66" not in (out.get("explanation") or "")
        assert "55" not in (out.get("explanation") or "")
        assert out.get("explanation"), "降级后仍应有确定性回复"
    finally:
        restore()


def test_contract_compliant_answer_zero_extra_calls(client: TestClient, db_session: Session):
    """路径 4：合规答案零额外调用（引用/打标与既有行为一致）。"""
    _seed(db_session)
    fake = _FakeLLM([
        _tool_call("vehicle_search", query="凯美瑞"),
        {"content": "丰田凯美瑞在售，官方指导价 20 万元。"},
    ])
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下凯美瑞现在的情况，给点官方信息"}).json()
        assert out["explanation"] == "丰田凯美瑞在售，官方指导价 20 万元。"
        assert out["filters"]["tool_loop"] is True
        assert out["filters"]["tool_calls"] == 1
        assert "contract_fallback" not in out["filters"]
        assert len(fake.calls) == 2, "合规答案不得触发重写（零额外调用）"
    finally:
        restore()


def test_contract_allows_restating_user_numbers(client: TestClient, db_session: Session):
    """评审二轮假阴性回归：复述用户自己说的数字不算编造，不得触发重写/降级。

    用户消息带「2025 年」；工具返回凯美瑞（200000 → 20 万口径）；最终回答复述
    「2025 年」「20 万元」。修复前 allowed_numbers 起点为空，2025 被判编造 →
    重写 → FakeLLM 脚本耗尽 → 降级；修复后一次调用直接放行。
    """
    _seed(db_session)
    fake = _FakeLLM([
        _tool_call("vehicle_search", brand="丰田"),
        {"content": "丰田 2025 年在售车型有凯美瑞，官方指导价 20 万元。"},
    ])
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下丰田 2025 年的在售情况"}).json()
        assert out["explanation"] == "丰田 2025 年在售车型有凯美瑞，官方指导价 20 万元。"
        assert out["filters"]["tool_loop"] is True
        assert "contract_fallback" not in out["filters"], (
            f"复述用户数字不得触发降级，实际 filters={out['filters']}"
        )
        assert len(fake.calls) == 2, "复述用户数字不得触发重写"
    finally:
        restore()
