"""会话历史注入 prompt 的**预算**（2026-10-21，修 P4 缺陷台账 M 项）。

## 缺陷

会话存储最多留 50 条消息、每条最多 2000 字 → 最坏 **100K 字**直接进 prompt。
中文 1 字 ≈ 1 token，64K 上下文的模型会被**直接拒收**（context length exceeded）
→ 用户看到的是降级回答，而不是他的问题；而且这份历史**每轮重发**，
成本与延迟随轮数线性涨。

## 守卫分两层

- **单元**（`select_history`）：短历史**逐条不动**（常见场景行为不变）；
  长历史按预算截断、保留最近的、不留半轮对话、至少留一条；
  截断时给出**明说**提示（静默截断会让模型以为看到了全部对话）。
- **接线**（engine）：真的按预算注入，且在 system 提示里说明省略了多少条。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.engine import AgentEngine
from app.agent.history_budget import (
    HISTORY_CHAR_BUDGET,
    select_history,
    truncation_note,
)
from app.agent.schemas import UserProfile


def _history(turns: int, size: int) -> list[dict]:
    """造一段历史：user/assistant 交替，每条 `size` 字。"""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "长" * size}
        for i in range(turns)
    ]


# ── 单元：select_history ────────────────────────────────────────────────────


def test_short_history_is_kept_verbatim():
    """常见场景（50 条 × 30 字 ≈ 1500 字）**逐条不动**——行为与接入前相同。"""
    history = _history(50, 30)
    kept, dropped = select_history(history)
    assert kept == history
    assert dropped == 0
    assert truncation_note(dropped) == ""


def test_long_history_is_capped_by_the_budget():
    """长历史必须被预算卡住——否则 100K 字直接进 prompt。"""
    history = _history(50, 2000)  # 100K 字
    kept, dropped = select_history(history)
    total = sum(len(m["content"]) for m in kept)
    assert total <= HISTORY_CHAR_BUDGET, f"注入的历史 {total} 字，超出预算"
    assert dropped > 0


def test_keeps_the_most_recent_messages():
    """截断时保留**最近**的（近因更重要）。"""
    history = _history(50, 2000)
    kept, _ = select_history(history)
    assert kept[-1] == history[-1], "最后一条必须保留"
    assert kept[0] == history[len(history) - len(kept)], "应保留连续的一段最近消息"


def test_never_keeps_a_half_turn():
    """不能留下「assistant 开口」的半轮对话——某些后端会拒收。

    窗口大小按预算算准：49 条 × 2000 字，预算 8000 → 只留最后 4 条
    （索引 45..48）；45 是奇数 → assistant → **正好落在半轮上**，
    修复逻辑必须被触发（否则本用例测的是空气）。
    """
    history = _history(49, 2000)
    # 前提：不含修复时，窗口第一条确实是 assistant
    raw = list(reversed(history))[:4]
    raw.reverse()
    assert raw[0]["role"] == "assistant", "前提不成立：窗口第一条不是 assistant"

    kept, _ = select_history(history)
    assert kept, "至少要留一条"
    assert kept[0]["role"] == "user", "第一条必须是 user（不留半轮）"
    assert len(kept) == 3, "开头的 assistant 应被丢掉"


def test_at_least_one_message_is_kept():
    """单条就超预算时也要留它——完全无上下文更糟。"""
    history = [{"role": "user", "content": "长" * 10000}]
    kept, dropped = select_history(history, budget=100)
    assert len(kept) == 1
    assert dropped == 0


def test_truncation_note_only_when_something_was_dropped():
    assert truncation_note(0) == ""
    note = truncation_note(7)
    assert "7" in note and "对话" in note


# ── 接线：engine 真的按预算注入 ─────────────────────────────────────────────


class _ScriptedLLM:
    """记录收到的每一条 message。"""

    available = True

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, msgs, tools=None, temperature=0.2, json_mode=False, thinking=None):
        self.calls.append(list(msgs))
        return {"choices": [{"message": {"content": self.replies.pop(0)}}]}


class _Store:
    """与真实 `SessionStore` 同形状（这三个方法都经 `run_in_threadpool` 调用，故为同步）。"""

    def __init__(self, history: list[dict]):
        self._history = list(history)
        self.profile = UserProfile()

    def get_profile(self, _sid):
        return self.profile.model_dump()

    def set_profile(self, _sid, data):
        self.profile = type(self.profile)(**data)
        return None

    def history(self, _sid):
        return list(self._history)


class _Engine(AgentEngine):
    def __init__(self, llm, history: list[dict]):  # noqa: D107 - 测试替身
        self._llm = llm
        self._store = _Store(history)


@pytest.fixture()
def _patch_retrieval(monkeypatch):
    monkeypatch.setattr("app.agent.engine.retrieval_search", lambda *a, **k: [])


def test_engine_prompt_respects_the_budget(monkeypatch, _patch_retrieval):
    """engine 真的按预算注入历史——接线点，不是纯函数。"""
    history = _history(50, 2000)  # 100K 字
    llm = _ScriptedLLM(["好的。"])
    engine = _Engine(llm, history)
    asyncio.run(engine._chat_reply(None, "sid", "现在的问题"))

    msgs = llm.calls[0]
    injected = msgs[1:-1]  # 去掉 system 与本轮提问
    total = sum(len(m["content"]) for m in injected)
    assert total <= HISTORY_CHAR_BUDGET, f"注入历史 {total} 字，超出预算"
    assert len(injected) < len(history), "长历史必须被截断"


def test_engine_prompt_says_how_many_were_dropped(monkeypatch, _patch_retrieval):
    """截断必须**明说**——静默截断会让模型以为看到了全部对话。"""
    history = _history(50, 2000)
    llm = _ScriptedLLM(["好的。"])
    engine = _Engine(llm, history)
    asyncio.run(engine._chat_reply(None, "sid", "现在的问题"))
    system = llm.calls[0][0]["content"]
    assert "条对话" in system and "未提供" in system, (
        f"system 提示里必须说明省略了多少条：{system[-200:]}"
    )


def test_engine_prompt_unchanged_for_short_history(monkeypatch, _patch_retrieval):
    """短历史时提示**一个字都不该多**——常见场景行为不变。"""
    history = _history(10, 30)
    llm = _ScriptedLLM(["好的。"])
    engine = _Engine(llm, history)
    asyncio.run(engine._chat_reply(None, "sid", "现在的问题"))
    msgs = llm.calls[0]
    assert "未提供" not in msgs[0]["content"]
    # 引擎注入的是 history[:-1]（最后一条即本轮提问，单独追加）→ system + (n-1) + 本轮
    assert len(msgs) == len(history) + 1
