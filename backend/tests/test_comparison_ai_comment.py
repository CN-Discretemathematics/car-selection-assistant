"""差异分析 LLM 点评测试：grounded 守卫（零数字/超长/不可用回退/缓存不重复计费）。"""
from __future__ import annotations

import asyncio

import pytest

import app.comparison.ai_summary as ai_summary
from app.comparison.ai_summary import ai_comment_for, build_comment_messages
from app.common.llm import LLMClient, LLMError


class _FakeClient:
    """可编程假客户端：available / 返回文本 / 调用计数（测试不触真实 LLM）。"""

    def __init__(self, text: str | None, available: bool = True, fail: bool = False):
        self._text = text
        self.available = available
        self._fail = fail
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.2, json_mode=False):
        self.calls += 1
        if self._fail:
            raise LLMError("网络失败")
        return {"choices": [{"message": {"content": self._text}}]}


def _analysis():
    return {
        "variants": [{"variant_id": 1, "label": "甲"}, {"variant_id": 2, "label": "乙"}],
        "verdict": "总体：甲 强在 动力；乙 强在 续航；后者指导价最低。",
        "summary": ["动力：甲 比 乙 高 50kW，差距 33%"],
        "tradeoffs": ["甲：在 动力 上领先"],
        "allowed_numbers": [50.0],
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    ai_summary._CACHE.clear()
    yield
    ai_summary._CACHE.clear()


def test_ai_comment_returns_grounded_text(monkeypatch):
    fake = _FakeClient("甲车动力更强，乙车续航更长，按你的用车场景选即可。")
    monkeypatch.setattr(ai_summary, "get_llm_client", lambda: fake)
    text = asyncio.run(ai_comment_for(_analysis()))
    assert text == "甲车动力更强，乙车续航更长，按你的用车场景选即可。"
    # prompt 必须显式约束数字与营销话术
    messages = build_comment_messages(_analysis())
    assert "只能使用事实里已经出现过的数字" in messages[0]["content"]
    assert "已核实事实" in messages[1]["content"]


def test_ai_comment_allows_numbers_present_in_facts(monkeypatch):
    """事实里出现过的数字可以复述（如价差 50kW / 差距 33%），不必强求零数字。"""
    fake = _FakeClient("甲车动力比乙车高 50kW（差距 33%），预算够就选甲。")
    monkeypatch.setattr(ai_summary, "get_llm_client", lambda: fake)
    text = asyncio.run(ai_comment_for(_analysis()))
    assert text is not None and "50kW" in text


def test_ai_comment_rejects_invented_numbers(monkeypatch):
    """事实之外的数字（编造/自行换算）= 失控，点评必须丢弃。"""
    fake = _FakeClient("甲车动力强 20%，建议选甲。")   # 20% 不在事实里（事实是 33%）
    monkeypatch.setattr(ai_summary, "get_llm_client", lambda: fake)
    assert asyncio.run(ai_comment_for(_analysis())) is None


def test_ai_comment_falls_back_when_unavailable(monkeypatch):
    fake = _FakeClient(None, available=False)
    monkeypatch.setattr(ai_summary, "get_llm_client", lambda: fake)
    assert asyncio.run(ai_comment_for(_analysis())) is None


def test_ai_comment_falls_back_on_llm_error(monkeypatch):
    fake = _FakeClient(None, available=True, fail=True)
    monkeypatch.setattr(ai_summary, "get_llm_client", lambda: fake)
    assert asyncio.run(ai_comment_for(_analysis())) is None


def test_ai_comment_rejects_oversized(monkeypatch):
    fake = _FakeClient("甲" * 120)
    monkeypatch.setattr(ai_summary, "get_llm_client", lambda: fake)
    assert asyncio.run(ai_comment_for(_analysis())) is None


def test_ai_comment_uses_cache_for_same_pair(monkeypatch):
    fake = _FakeClient("甲车动力更强，乙车续航更长，按你的用车场景选即可。")
    monkeypatch.setattr(ai_summary, "get_llm_client", lambda: fake)
    asyncio.run(ai_comment_for(_analysis()))
    asyncio.run(ai_comment_for(_analysis()))
    assert fake.calls == 1, "同组款型应命中缓存，不重复计费"


def test_build_messages_skips_when_no_facts():
    assert build_comment_messages({"variants": []}) is None
    assert build_comment_messages({}) is None


def test_llm_client_unavailable_by_default_key():
    """与既有约定一致：密钥为空 → available=False（确定性回退的前提）。"""
    assert LLMClient(api_key="").available is False
