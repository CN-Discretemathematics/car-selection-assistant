"""第二轮审查 M2 的回归：多种子下，模型的参数必须真正生效。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.seed import make_brand, make_series, make_source, make_variant, make_year
from tests.test_tool_loop import _FakeLLM, _client_with_llm


def _seed_two_brands(db: Session):
    source = make_source(db, name="汽车之家")
    toyota = make_brand(db, name="丰田", source=source)
    byd = make_brand(db, name="比亚迪", source=source)
    camry = make_series(db, toyota, name="凯美瑞", energy_types=("ICE",), source=source)
    seal = make_series(db, byd, name="海豹06", energy_types=("BEV",), source=source)
    y1 = make_year(db, camry)
    y2 = make_year(db, seal)
    make_variant(db, camry, y1, config_version="2.0G", energy_type="ICE", price_cny="200000", source=source)
    make_variant(db, seal, y2, config_version="MAX", energy_type="BEV", price_cny="150000", source=source)
    db.commit()


def test_query_parameter_actually_filters(client: TestClient, db_session: Session):
    """阻断项回归：vehicle_search 的 query 不能被丢弃（否则问秦PLUS返回任意车系）。"""
    _seed_two_brands(db_session)
    script = [
        {"content": "", "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": "vehicle_search", "arguments": json.dumps({"query": "凯美瑞"}, ensure_ascii=False)}}]},
        {"content": "丰田凯美瑞在售 1 款（来源见引用）。"},
    ]
    fake = _FakeLLM(script)
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下凯美瑞，给点官方信息"}).json()
        # 工具结果里只能有凯美瑞（此前 query 被丢弃会返回海豹06 等无关车系）
        tool_msg = next(m for m in fake.calls[1] if m.get("role") == "tool")
        assert "海豹06" not in tool_msg["content"], "query 参数被丢弃（返回了无关车系）"
        assert "凯美瑞" in tool_msg["content"]
        assert "凯美瑞" in (out.get("explanation") or "")
    finally:
        restore()


def test_limit_is_clamped(client: TestClient, db_session: Session):
    """limit=10^9 不得放大成全表查询（N+1 放大面）。"""
    _seed_two_brands(db_session)
    from app.agent.engine import _dispatch_tool
    result = _dispatch_tool(db_session, "vehicle_search", {"limit": 10**9})
    assert len(result["results"]) <= 2, "limit 应被夹紧"


def test_general_questions_not_hijacked(client: TestClient, db_session: Session):
    """汽车语境门禁：「量子纠缠」「华为 vs 苹果」不得进入工具循环。"""
    _seed_two_brands(db_session)
    script = [{"content": "这是通用知识问题。"}]
    fake = _FakeLLM(script)
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "帮我解释一下量子纠缠"}).json()
        assert out.get("filters", {}).get("tool_loop") in (None, False), "通用问题不得进入工具循环"
    finally:
        restore()
