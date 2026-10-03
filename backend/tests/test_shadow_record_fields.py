"""路由 shadow 记录的两个数据质量问题（2026-10-02）。

1. `llm_elapsed_ms` 原先只计**最后一次**尝试（`started` 写在重试循环体内），
   重试的退避睡眠（0.8s + 2.5s）被整段排除 → shadow_report 的 p50/p95
   **系统性低估**重试样本的路由开销。现同时记录总墙钟与最后一次尝试耗时。

   ⚠️ 影响范围要说准：llm_router.py 切流判据 4 用的是**回答级**耗时日志
   （logger "app.agent.respond"），**不受**本字段影响；这里只影响 shadow 诊断。

2. 路由 LRU 的 key 原先只有 `model::utterance`。模型没换、但提示词改了
   （few-shot 增删、金标改判）时，旧裁决会一直活到进程重启，
   shadow 对拍数据混入旧提示词下的判定，跨版本比较失真。
   现把 `ROUTER_VERSION` 并入 key：改提示词的人顺手 +1 即自然失效。
"""
from __future__ import annotations

import json
import logging

from app.agent.llm_router import ROUTER_VERSION, _cache_key, log_shadow_record


def test_cache_key_contains_router_version():
    """改提示词时 +1 版本号即让旧裁决失效。"""
    key = _cache_key("我想买台车", "deepseek-chat")
    assert ROUTER_VERSION in key
    assert "deepseek-chat" in key
    assert "我想买台车" in key


def test_cache_key_varies_with_model():
    """换模型仍不命中（v2.2 已有行为，勿回退）。"""
    assert _cache_key("你好", "model-a") != _cache_key("你好", "model-b")


def test_cache_key_normalizes_utterance():
    """首尾空白 + 连续空白折叠为单空格（v2.2 已有行为，勿回退）。

    注意归一是「折叠成单空格」而非「删除」：`"我   想"` 归一为 `"我 想"`，
    与 `"我想"` 仍是不同的 key——这是既有行为，本轮不改变它。
    """
    assert _cache_key("  我   想买车  ", "m") == _cache_key("我 想买车", "m")
    assert _cache_key("HELLO", "m") == _cache_key("hello", "m"), "key 必须大小写无关"


def test_shadow_record_carries_both_timings(caplog):
    """记录里同时有总墙钟与最后一次尝试耗时，口径可区分。"""
    with caplog.at_level(logging.INFO, logger="app.agent.router.shadow"):
        log_shadow_record(
            message="我想买台车",
            regex_intent="recommendation",
            llm_decision=None,
            llm_elapsed_ms=3300.0,      # 含退避
            llm_last_attempt_ms=400.0,  # 单次
        )
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["llm_elapsed_ms"] == 3300.0
    assert payload["llm_last_attempt_ms"] == 400.0
    assert payload["llm_elapsed_ms"] > payload["llm_last_attempt_ms"], (
        "总墙钟必须不小于单次尝试，否则说明退避仍被排除在外"
    )


def test_shadow_record_omits_last_attempt_when_not_given(caplog):
    """不传该字段时不写入（保持与旧记录可比，不伪造 0）。"""
    with caplog.at_level(logging.INFO, logger="app.agent.router.shadow"):
        log_shadow_record(
            message="x",
            regex_intent="chitchat",
            llm_decision=None,
            llm_elapsed_ms=1.0,
        )
    payload = json.loads(caplog.records[-1].getMessage())
    assert "llm_last_attempt_ms" not in payload
