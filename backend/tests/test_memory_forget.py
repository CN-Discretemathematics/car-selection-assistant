"""记忆删除的**自然语言入口**（2026-10-21，Phase 1 的最后一块）。

计划 §3.3 的第三个场景「记忆删除后不再生效」的题面就是一句大白话：
「把之前记的预算忘掉」——本笔让这句话真的生效。

## 为什么是「用户入口」而不是再做一个 API

删除记忆的**存储层**（`preferences.forget`）与**设置页**的撤回端点
（`DELETE /agent/preferences/{memory_id}`）在 Phase 1 里已经有了 ✓。
缺的是**对话里的那句话**——评测题面用的就是它。

## 两处口径差别（**刻意不同**，写在这里免得后人当漂移）

- 设置页撤回 = 改一条**记录**；当前会话已谈定的条件不动（该端点 docstring 已写明）。
- 对话里说「忘掉」= **当下指令**：回执既要说「已忘掉」，就必须**真的不再用**——
  否则下一句推荐还按那个预算走，回执就是假话 ⇒ 会话画像里对应字段也一并清掉。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.preferences import (
    MODE_ENV,
    TRACKED_KEYS,
    detect_forget_request,
    forget_keys,
    forget_reply,
    get_or_create_identity,
    list_memories,
    remember,
)


@pytest.fixture()
def durable_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开持久化闸门（默认 off，测试必须显式打开）。"""
    monkeypatch.setenv(MODE_ENV, "on")


def _identity(db_session: Session):
    identity, _token, created = get_or_create_identity(db_session)
    db_session.commit()
    assert created is True
    return identity


def _identity_headers(client: TestClient) -> dict[str, str]:
    ident = client.post("/api/v1/agent/identity").json()
    return {"Authorization": f"Bearer {ident['token']}"}


def _create_session(client: TestClient) -> str:
    resp = client.post("/api/v1/agent/sessions")
    assert resp.status_code == 201
    return resp.json()["session_id"]


# ── 识别 ────────────────────────────────────────────────────────────────────


def test_detect_recognizes_the_subject():
    """「把之前记的预算忘掉」→ 预算键。"""
    assert detect_forget_request("把之前记的预算忘掉") == ["budget"]
    assert detect_forget_request("别记我的用途了") == ["usage"]
    assert detect_forget_request("删掉上次说的人数") == ["passengers"]


def test_detect_needs_both_the_verb_and_a_memory_reference():
    """**两者都要有**——否则「删除 SUV」这种约束变更会被误判成删记忆。"""
    assert detect_forget_request("删除SUV") == []
    assert detect_forget_request("预算20万") == []
    assert detect_forget_request("不要电车") == []
    assert detect_forget_request("别记了") == []


def test_detect_all():
    """「把之前记的都忘掉」→ 全部被跟踪的键。"""
    assert set(detect_forget_request("把之前记的都忘掉")) == {t.key for t in TRACKED_KEYS}


# ── 执行 ────────────────────────────────────────────────────────────────────


def test_forget_removes_the_durable_row_only(db_session: Session, durable_on):
    """删掉**跨会话记忆**里那个键的行；**会话画像不动**。

    与设置页的撤回端点（`DELETE /agent/preferences/{id}`）同一语义：
    「记忆」是跨会话的，当前对话里已谈定的条件属于**会话状态**（可在会话里重置）。
    两处口径不许分叉 ⇒ 这里也不清会话画像，回执负责说清范围。
    """
    identity = _identity(db_session)
    remember(
        db_session, identity, category="constraint", key="budget",
        value='{"min": 200000, "max": 250000}', confirmed=True,
    )
    db_session.commit()
    assert [m.key for m in list_memories(db_session, identity)] == ["budget"]

    cleared = forget_keys(db_session, identity, ["budget"])
    db_session.commit()

    assert cleared == ["budget"]
    assert list_memories(db_session, identity) == []


def test_forget_reply_is_visible():
    """回执要说清**范围**——删的是跨会话记忆，当前对话里的条件仍在。

    范围不说清就是骗人：用户以为「忘掉了」，这段对话里却还按那个预算推荐。
    """
    reply = forget_reply(["budget"])
    assert "预算" in reply
    assert "这段对话里" in reply, reply
    assert "用途" in forget_reply(["usage", "passengers"]).split("——")[0]
    assert forget_reply([]) == "你之前记的条件里没有这一项。"


# ── 端到端（对话里那句话真的生效）──────────────────────────────────────────


def test_forget_turn_clears_the_memory_and_the_next_session(
    client: TestClient, db_session: Session, durable_on
):
    """题面那句话要真的生效：

    1. 第一轮记住预算；
    2. 第二轮说「把之前记的预算忘掉」→ 回执可见 + 记忆行消失；
    3. 第三轮（新会话）不再按那个预算筛（= 分支 7 刚修的 `budget_not_in` 断言要验的）。
    """
    headers = _identity_headers(client)

    sid1 = _create_session(client)
    client.post(
        f"/api/v1/agent/sessions/{sid1}/messages",
        json={"message": "预算20到25万，通勤，两三个人"},
        headers=headers,
    )
    rows = client.get("/api/v1/agent/preferences", headers=headers).json()
    assert any(r["key"] == "budget" for r in rows), rows

    # 第二轮：同一会话里说「忘掉」
    resp = client.post(
        f"/api/v1/agent/sessions/{sid1}/messages",
        json={"message": "把之前记的预算忘掉"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "预算" in (body.get("explanation") or ""), body
    rows = client.get("/api/v1/agent/preferences", headers=headers).json()
    assert not any(r["key"] == "budget" for r in rows), rows

    # 第三轮：新会话里不再有那个预算
    sid2 = _create_session(client)
    third = client.post(
        f"/api/v1/agent/sessions/{sid2}/messages",
        json={"message": "现在推荐一台车"},
        headers=headers,
    ).json()
    filters = third.get("filters") or {}
    assert filters.get("budget_min") is None, filters
    assert filters.get("budget_max") is None, filters


def test_normal_turn_is_not_treated_as_a_forget_request(
    client: TestClient, db_session: Session, durable_on
):
    """普通诉求不得被当成删除请求（否则用户的预算会被无声清掉）。"""
    headers = _identity_headers(client)
    sid = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "预算20到25万，通勤，两三个人"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    rows = client.get("/api/v1/agent/preferences", headers=headers).json()
    assert any(r["key"] == "budget" for r in rows), rows


def test_forget_is_skipped_without_an_identity(client: TestClient, db_session: Session, durable_on):
    """没有身份（`DURABLE_PREFS_MODE=off`）→ 记忆功能整体不存在，行为逐位不变。"""
    sid = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "把之前记的预算忘掉"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "已忘掉" not in (body.get("explanation") or ""), body
