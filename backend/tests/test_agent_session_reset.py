"""会话记忆重置接口测试（画像在会话内累积，必须有正式重置入口）。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_agent import _seed_agent_data


def _converse(client: TestClient, sid: str) -> dict:
    """走一轮能形成画像的对话（预算/用途/人数）。"""
    last: dict = {}
    for message in ("预算15万，家庭用车，5口人，想要新能源SUV",):
        last = client.post(f"/api/v1/agent/sessions/{sid}/messages", json={"message": message}).json()
    return last


def test_reset_clears_profile_and_history(client: TestClient, db_session: Session):
    """重置后：画像清空（下一轮重新追问）、历史清空、会话仍可用。"""
    _seed_agent_data(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    first = _converse(client, sid)
    assert first.get("filters", {}).get("budget_max") == 150000
    assert client.get(f"/api/v1/agent/sessions/{sid}/stream").status_code == 200

    assert client.post(f"/api/v1/agent/sessions/{sid}/reset").status_code == 204

    # 会话仍存在（不是删除），但画像与上次结果都已清空
    stream = client.get(f"/api/v1/agent/sessions/{sid}/stream")
    assert stream.status_code == 200 and "event: message" not in stream.text
    again = client.post(
        f"/api/v1/agent/sessions/{sid}/messages", json={"message": "我想买台车"}
    ).json()
    assert again["filters"] == {}, "重置后不应残留上一轮的预算等约束"
    assert again["need_clarification"] is True, "画像清空后应重新追问预算"


def test_delete_session_then_requests_are_404(client: TestClient, db_session: Session):
    _seed_agent_data(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    assert client.delete(f"/api/v1/agent/sessions/{sid}").status_code == 204
    assert client.post(f"/api/v1/agent/sessions/{sid}/messages", json={"message": "你好"}).status_code == 404
    assert client.post(f"/api/v1/agent/sessions/{sid}/reset").status_code == 404


def test_reset_unknown_session_is_404(client: TestClient):
    assert client.post("/api/v1/agent/sessions/nonexistent/reset").status_code == 404
    assert client.delete("/api/v1/agent/sessions/nonexistent").status_code == 404
