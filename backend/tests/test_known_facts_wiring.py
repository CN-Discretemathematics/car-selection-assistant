"""P0-3 的**接线**测试（审查 H1：此前三个调用点零覆盖）。

审查实测：把 `false_denial_hits` 的返回值、`build_known_facts` 的入参、
`last_recommended_variant_ids` 的赋值三者分别短路，**全仓测试仍然全绿**。
即 `known_facts` 模块可以在完全不生效的情况下合入——而我的反向验证 9/9
只证明了那两个纯函数有牙齿。

这与我在 P0-2 / P1-2 上刚犯又刚修好的错误**是同一类**：只测纯函数，不测接线。

因此本组直接从 `_chat_reply` 的**外部可观测行为**切入：
- 模型问参数时：事实块必须出现在它看到的上下文里
- 模型说有据却否认时：必须触发改写，且改写后不得仍是假否定
- 模型两次都否认时：兜底不得把**系统指令**泄露给用户
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent import known_facts
from app.agent.engine import AgentEngine

BLOCK = (
    "【已推荐车型的库内参数（直接来自数据库，权威，可直接引用）】\n"
    "下列参数确实存在于库中；被问到这些参数时**只能依据这里作答**，不得回答「未披露」。\n"
    "- 捷途旅行者C-DM 2026款：最大功率(kW)=280；最大扭矩(N·m)=610"
)


class _ScriptedLLM:
    """按脚本逐次返回固定文本的假 LLM；记录收到的每一条 message。"""

    available = True

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, msgs, tools=None, temperature=0.2, json_mode=False, thinking=None):
        self.calls.append(list(msgs))
        return {"choices": [{"message": {"content": self.replies.pop(0)}}]}


class _Store:
    """与真实 `SessionStore` 同形状：这三个方法都经 `run_in_threadpool` 调用，故为**同步**。"""

    def __init__(self):
        from app.agent.schemas import Budget, UserProfile

        self.profile = UserProfile(
            budget=Budget(max=200000), last_recommended_variant_ids=[4242]
        )

    def get_profile(self, _sid):
        return self.profile.model_dump()

    def set_profile(self, _sid, data):
        self.profile = type(self.profile)(**data)
        return None

    def history(self, _sid):
        return []


class _Engine(AgentEngine):
    def __init__(self, llm):  # noqa: D107 - 测试替身
        self._llm = llm
        self._store = _Store()


@pytest.fixture()
def _patch_retrieval(monkeypatch):
    monkeypatch.setattr("app.agent.engine.retrieval_search", lambda *a, **k: [])


def _patch_facts(monkeypatch, *, with_data=True):
    """默认装上真实事实；`with_data=False` 用于模拟「短路入参 → 拿不到事实」。"""

    def _fake(_db, ids):
        if with_data and ids:
            return BLOCK, {"power"}
        return "", set()

    monkeypatch.setattr(known_facts, "build_known_facts", _fake)


def test_chat_reply_preloads_known_facts_into_context(monkeypatch, _patch_retrieval):
    """接线点 1：`_chat_reply` 必须真的把库内事实摆进模型上下文。

    短路入参（传 []）→ 模型看不见数据 → 假否定复现。
    """
    _patch_facts(monkeypatch)
    llm = _ScriptedLLM(["这几款最大功率分别是 280kW 和 335kW。"])
    out = asyncio.run(_Engine(llm)._chat_reply(None, "sid", "这几款的动力怎么样"))

    system = llm.calls[0][0]["content"]
    assert "最大功率(kW)=280" in system, "库内事实必须出现在模型看到的上下文里"
    assert "权威" in system
    assert "280kW" in out


def test_chat_reply_skips_preload_when_no_facts(monkeypatch, _patch_retrieval):
    """反向对照：拿不到事实时上下文里**不该**出现那块内容（否则是凭空注入）。"""
    _patch_facts(monkeypatch, with_data=False)
    llm = _ScriptedLLM(["这几款最大功率 280kW。"])
    asyncio.run(_Engine(llm)._chat_reply(None, "sid", "这几款的动力怎么样"))
    assert "最大功率(kW)=280" not in llm.calls[0][0]["content"]


def test_chat_reply_rewrites_when_model_denies(monkeypatch, _patch_retrieval):
    """接线点 2：模型说有据却否认 → 必须触发一次改写。"""
    _patch_facts(monkeypatch)
    llm = _ScriptedLLM([
        "抱歉，我手头暂时没有这几款车的动力参数。",
        "这几款最大功率 280kW，扭矩 610N·m，动力都够用。",
    ])
    out = asyncio.run(_Engine(llm)._chat_reply(None, "sid", "这几款的动力怎么样"))

    assert len(llm.calls) == 2, "假否定必须触发改写"
    assert "重新回答" in llm.calls[1][-1]["content"]
    assert "280kW" in out


def test_chat_reply_no_rewrite_when_answer_is_fine(monkeypatch, _patch_retrieval):
    """反向对照：正常回答不得被改写（守卫误伤会让这里多出一次调用）。"""
    _patch_facts(monkeypatch)
    llm = _ScriptedLLM(["这几款最大功率 280kW，扭矩 610N·m。"])
    asyncio.run(_Engine(llm)._chat_reply(None, "sid", "这几款的动力怎么样"))
    assert len(llm.calls) == 1


def test_chat_reply_fallback_never_leaks_instructions(monkeypatch, _patch_retrieval):
    """接线点 3：两次都否认 → 兜底只拼事实行，**不得**泄露块头指令。

    块头含「只能依据这里作答 / 不得回答未披露」——这是给模型的系统指令，
    泄露给用户既荒唐又暴露内部实现，而 safety_guard 只拦优惠/库存/成交。
    """
    _patch_facts(monkeypatch)
    llm = _ScriptedLLM([
        "我手头暂时没有这几款车的动力参数。",
        "还是查不到这几款的动力数据。",
    ])
    out = asyncio.run(_Engine(llm)._chat_reply(None, "sid", "这几款的动力怎么样"))

    assert len(llm.calls) == 2
    assert "最大功率" in out, "兜底必须直给事实"
    assert "只能依据这里作答" not in out, "不得把系统指令泄露给用户"
    assert "不得回答" not in out
    assert "权威" not in out
