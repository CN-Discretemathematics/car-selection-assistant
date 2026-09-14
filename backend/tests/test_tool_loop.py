"""LLM 工具调用循环测试（盘点/对比/解释类自由提问，2026-09-14 与用户确认开放）。

要点：步数上限、工具结果回灌、未知工具容错、护栏兜底；确定性链路仍是默认。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.engine import TOOL_LOOP_MAX_STEPS, _dispatch_tool, asks_tool_assist
from app.common.llm import LLMError
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


class _FakeLLM:
    """按脚本返回的假 LLM：记录每次调用的 msgs，便于断言工具结果是否回灌。"""

    def __init__(self, script: list[dict]):
        self.available = True
        self.script = list(script)
        self.calls: list[list[dict]] = []

    async def chat(self, msgs, tools=None, temperature=0.2, json_mode=False):
        self.calls.append([dict(m) for m in msgs])
        if not self.script:
            raise LLMError("脚本耗尽")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"choices": [{"message": item}]}


def _seed(db: Session):
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="丰田", source=source)
    series = make_series(db, brand, name="凯美瑞", energy_types=("ICE",), source=source)
    year = make_year(db, series)
    make_variant(db, series, year, config_version="2.0G", energy_type="ICE",
                 price_cny="200000", source=source)
    db.commit()
    return series.id


def _client_with_llm(client: TestClient, db_session: Session, fake: _FakeLLM):
    from app.agent.engine import get_agent_engine

    engine = get_agent_engine()
    original = engine._llm
    engine._llm = fake
    return lambda: setattr(engine, "_llm", original)


def test_intent_and_dispatch():
    assert asks_tool_assist("对比一下秦PLUS和海豹06")
    assert asks_tool_assist("解释一下什么是增程式")
    assert asks_tool_assist("10万以内有哪些车")
    assert not asks_tool_assist("你好")


def test_dispatch_coerces_and_drops_unknown_keys(db_session: Session):
    _seed(db_session)
    result = _dispatch_tool(db_session, "vehicle_search", {"query": "凯美瑞", "limit": "5", "evil": "x"})
    assert result["results"], "应能查到凯美瑞"
    assert result["results"][0]["series_name"] == "凯美瑞"
    assert _dispatch_tool(db_session, "nonexistent", {})["error"]


def test_tool_loop_executes_and_returns_final(client: TestClient, db_session: Session):
    """模型先调 vehicle_search（索引外品牌也能查），结果回灌后给出最终答案。"""
    _seed(db_session)
    fake = _FakeLLM([
        {"content": "", "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": "vehicle_search", "arguments": json.dumps({"query": "凯美瑞"}, ensure_ascii=False)}}]},
        {"content": "丰田凯美瑞在售，官方指导价 20 万元（来源：汽车之家）。"},
    ])
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下凯美瑞现在的情况，给点官方信息"}).json()
        assert "凯美瑞" in (out.get("explanation") or "")
        assert out["filters"].get("tool_loop") is True and out["filters"]["tool_calls"] == 1
        # 工具结果必须回灌（第二条消息含 role=tool）
        tool_roles = [m for m in fake.calls[1] if m.get("role") == "tool"]
        assert tool_roles and "凯美瑞" in tool_roles[0]["content"]
    finally:
        restore()


def test_tool_loop_step_cap_falls_back(client: TestClient, db_session: Session):
    """模型无限要求调工具：达到步数上限后必须回退普通对话，绝不挂死。"""
    _seed(db_session)
    endless = {"content": "", "tool_calls": [{"id": "x", "type": "function",
               "function": {"name": "sales_search", "arguments": "{}"}}]}
    fake = _FakeLLM([endless] * (TOOL_LOOP_MAX_STEPS + 4))
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下这两年新能源为什么涨价"}).json()
        assert out.get("explanation"), "超步数后应有兜底回复"
        # 步数上限 + 1（强制的最终回答调用）+ 1（回退路径自身的对话调用）
        assert len(fake.calls) <= TOOL_LOOP_MAX_STEPS + 2
        # 回退必须可观测（2026-09-15 新增：此前回退后 filters 为空，无法区分「没进循环」与「进过被拒」）
        assert out["filters"].get("tool_loop") is False
        assert out["filters"].get("tool_loop_fallback")
    finally:
        restore()


def test_tool_loop_forced_final_answer(client: TestClient, db_session: Session):
    """步数用尽后强制要一次最终回答：模型不再拿到工具，只能给答案。

    实测（2026-09-15 人工复现）：模型会连续 4 轮调 8 次工具仍不给答案，
    没有这一步整轮白跑、只能回退通用对话。
    """
    _seed(db_session)
    endless = {"content": "", "tool_calls": [{"id": "x", "type": "function",
               "function": {"name": "vehicle_search", "arguments": "{}"}}]}
    fake = _FakeLLM([endless] * TOOL_LOOP_MAX_STEPS + [{"content": "库内共 1 款在售车型（来源见引用）。"}])
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "盘点一下现在有哪些车"}).json()
        assert "1 款" in (out.get("explanation") or ""), "强制最终回答应被采用"
        assert out["filters"].get("tool_loop") is True
    finally:
        restore()


def test_tool_loop_safety_guard_falls_back(client: TestClient, db_session: Session):
    """最终答案触发护栏（如「最低价」）时回退普通对话。"""
    _seed(db_session)
    fake = _FakeLLM([
        {"content": "最低价 19.98 万，优惠信息可以谈。"},  # safety_guard 命中「最低价」
    ])
    restore = _client_with_llm(client, db_session, fake)
    try:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        out = client.post(f"/api/v1/agent/sessions/{sid}/messages",
                          json={"message": "解释一下凯美瑞的价格构成"}).json()
        assert out.get("explanation"), "护栏命中后应有兜底回复"
        assert "优惠信息可以谈" not in (out.get("explanation") or "")
    finally:
        restore()
