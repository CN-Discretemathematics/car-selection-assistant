# -*- coding: utf-8 -*-
"""LLM 结构化路由器 + 影子模式测试（Agent 理解层智能化 W0 Phase 2）。

覆盖（对应实现 app/agent/llm_router.py + engine 接线 + app/agent/shadow_report.py）：
- llm 模式：合法高置信裁决 → intent 被 LLM 改写，端到端断言执行走向 LLM 意图分支；
- 兜底：非法 JSON / intent 越权 / 硬超时 / 低置信 → 回退 regex 决策（打 router_fallback）；
- shadow 模式：响应与 regex 模式**完全一致**；LLM 拖慢 1.5s 响应耗时仍不含 LLM 等待
  （用 logger "app.agent.respond" 回答级耗时日志断言）；对拍记录落到
  logger "app.agent.router.shadow"；
- 缓存：同 utterance 第二次路由不触发 LLM（fake 调用计数）；
- regex 默认模式：完全不调 LLM（零新行为）；
- shadow_report：聚合口径（无裁决不进一致率分母）+ markdown + CLI 往返。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.engine import _SHADOW_RETRY_DELAYS, get_agent_engine
from app.agent.llm_router import (
    _ROUTER_SYSTEM_PROMPT,
    _cache_key,
    _router_client,
    ROUTER_VERSION,
    arbitrate_route,
    clear_route_cache,
    get_router_min_confidence,
    get_router_mode,
    get_router_thinking,
    get_router_timeout_ms,
    normalize_utterance,
    reset_router_client,
    route_with_llm,
)
from app.agent.schemas import UserProfile
from app.agent.routing import RouteDecision
from app.catalog.series_index import resolve_series
from app.common.llm import LLMError, _chat_payload
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

# regex 判 catalog_count（金标行）；FakeLLM 改判 chitchat —— 执行分支可从 filters 区分
CATALOG_ASK = "全部车型有多少款车？"


class _RouterFakeLLM:
    """路由测试专用假 LLM（FakeLLM 模式参考 tests/test_tool_loop.py）。

    json_mode=True（路由调用）→ 返回脚本裁决文本（可拖慢 router_latency 秒）；
    其余调用（回答链路）→ 抛 LLMError，让回答落到确定性模板——保证 shadow/llm
    模式下回答内容与「无 LLM 的 regex 模式」可比。
    """

    def __init__(self, verdict: str | None = None, router_latency: float = 0.0,
                 fail_first: int = 0):
        self.available = True
        self.verdict = verdict
        self.router_latency = router_latency
        self.fail_first = fail_first
        self.calls: list[dict] = []
        self.router_calls = 0

    async def chat(self, msgs, tools=None, temperature=0.2, json_mode=False, thinking=None):
        self.calls.append({"json_mode": json_mode, "n_msgs": len(msgs), "thinking": thinking})
        if not json_mode:
            raise LLMError("非路由调用：回答链路应回退确定性模板")
        self.router_calls += 1
        if self.fail_first and self.router_calls <= self.fail_first:
            raise LLMError("模拟瞬态网络失败（DeepSeek 并发重置复现）")
        if self.router_latency:
            await asyncio.sleep(self.router_latency)
        return {"choices": [{"message": {"content": self.verdict}}]}


class _UnavailableLLM:
    """available=False 的客户端：路由必须短路返回 None，不得发起调用。"""

    available = False

    async def chat(self, msgs, **kwargs):  # pragma: no cover - 断言不应到达
        raise AssertionError("LLM 不可用时不得发起调用")


@pytest.fixture(autouse=True)
def _fresh_router_cache():
    """路由缓存是模块级进程内状态：每个测试前后清空，杜绝跨测试串味。"""
    clear_route_cache()
    yield
    clear_route_cache()


def _verdict(intent: str, confidence: float) -> str:
    return json.dumps({"intent": intent, "slots": {}, "confidence": confidence}, ensure_ascii=False)


def _seed(db: Session) -> None:
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="丰田", source=source)
    series = make_series(db, brand, name="凯美瑞", energy_types=("ICE",), source=source)
    year = make_year(db, series)
    make_variant(db, series, year, config_version="2.0G", energy_type="ICE",
                 price_cny="200000", source=source)
    db.commit()


# ── route_with_llm 单元（FakeLLM 直连，不经 HTTP）────────────────────────────

def test_route_with_llm_adopts_valid_high_confidence_verdict():
    fake = _RouterFakeLLM(_verdict("tool_loop", 0.9))
    decision = asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    ))
    assert decision is not None
    assert decision.intent == "tool_loop"                       # intent 允许被 LLM 改写
    assert decision.matched_rule == "llm-router"
    assert decision.slots == {}                                 # slots 一律不许来自 LLM
    assert decision.signals["confidence"] == pytest.approx(0.9)
    assert decision.signals["regex_intent"] == "catalog_count"  # 对拍意图


def test_route_with_llm_rejects_invalid_json():
    fake = _RouterFakeLLM("这不是 JSON {{{")
    assert asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    )) is None


def test_route_with_llm_rejects_out_of_allowlist_intent():
    fake = _RouterFakeLLM(_verdict("delete_all_rows", 0.99))
    assert asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    )) is None
    assert fake.router_calls == 1


@pytest.mark.parametrize(
    "content",
    [
        '{"intent":"catalog_count","confidence":NaN,"slots":{}}',   # NaN
        '{"intent":"catalog_count","confidence":true,"slots":{}}',  # bool 冒充数值
        '{"intent":"catalog_count","confidence":1.5,"slots":{}}',   # 越上界
        '{"intent":"catalog_count","confidence":-0.1,"slots":{}}',  # 越下界
        '{"intent":"catalog_count","slots":{}}',                    # 缺 confidence
        '{"intent":"catalog_count","confidence":"0.9","slots":{}}', # 字符串置信
        '{"intent":"catalog_count","confidence":0.9,"slots":[]}',   # slots 非 dict
        '{"intent":"catalog_count","confidence":0.9}',              # 缺 slots
        '{"confidence":0.9,"slots":{}}',                            # 缺 intent
        '{"intent":123,"confidence":0.9,"slots":{}}',               # intent 非字符串
        "垃圾前缀 {\"intent\":\"catalog_count\",\"confidence\":0.9,\"slots\":{}} 垃圾后缀",
    ],
)
def test_route_with_llm_rejects_malformed_verdicts(content: str):
    """评审三轮非阻塞 4：13 种非法输出逐条钉住——实现必须全部回退 None。"""
    fake = _RouterFakeLLM(content)
    assert asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
        min_confidence=0.0,
    )) is None


def test_route_with_llm_logs_invalid_output_head(caplog):
    """输出不合法时记 INFO 归因日志（掩码+截断）——生产首轮 21 条无裁决无法归因的补课。

    断言两点：回退行为不变（None）；日志携带原始输出头部供下一轮提示词迭代归因。
    """
    fake = _RouterFakeLLM("您好！我判断这条消息应该是咨询购车意向。")
    with caplog.at_level(logging.INFO, logger="app.agent.router"):
        decision = asyncio.run(route_with_llm(
            CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
        ))
    assert decision is None
    assert any("输出不合法" in r.message and "购车意向" in r.message for r in caplog.records)


def test_router_system_prompt_pins_enum_and_boundary_examples():
    """提示词 v2 纪律：8 个枚举值必须逐字出现（防编辑时漏掉）；分歧集中区的金标
    few-shot 锚点必须在场。金标改判时本测试与提示词示例需同步更新。"""
    from app.agent.routing import INTENTS

    for name in INTENTS:
        assert name in _ROUTER_SYSTEM_PROMPT, f"提示词缺少枚举值 {name}"
    for anchor in ("款型ID：11、12、13", "汉兰达有几个版本", "车系的销量是多少",
                   "哪个品牌车型数量最多", "电动车和油车哪个好"):
        assert anchor in _ROUTER_SYSTEM_PROMPT, f"提示词缺少金标边界示例 {anchor}"


# ── 思考档位（DeepSeek Thinking Mode）────────────────────────────────────────
# 背景（2026-09-18）：deepseek-flash 默认 enabled+effort=high，路由为 8 选 1 分类
# 却付思考税（HIGH 档 p50=814ms、1 条 1525ms 超时截断）；默认改 disabled，
# 档位经 AGENT_ROUTER_THINKING 可调（用户问「降低思考强度能否提速」的落地）。

def test_chat_payload_thinking_semantics():
    """请求体构造三态：None 不发参数（端点默认）；disabled 关思考；low/max 开思考+档位。"""
    msgs = [{"role": "user", "content": "x"}]
    p = _chat_payload("m", msgs, None, 0.2, True, None)
    assert "thinking" not in p and "reasoning_effort" not in p
    p = _chat_payload("m", msgs, None, 0.2, True, "disabled")
    assert p["thinking"] == {"type": "disabled"} and "reasoning_effort" not in p
    p = _chat_payload("m", msgs, None, 0.2, True, "low")
    assert p["thinking"] == {"type": "enabled"} and p["reasoning_effort"] == "low"
    p = _chat_payload("m", msgs, None, 0.2, True, "max")
    assert p["reasoning_effort"] == "max"
    with pytest.raises(ValueError):
        _chat_payload("m", msgs, None, 0.2, True, "yolo"), "非法值构建期响亮失败"


def test_route_with_llm_sends_thinking_disabled_by_default():
    fake = _RouterFakeLLM(_verdict("tool_loop", 0.9))
    asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    ))
    assert fake.calls[0]["thinking"] == "disabled"


def test_router_thinking_env(monkeypatch):
    monkeypatch.setenv("AGENT_ROUTER_THINKING", "low")
    fake = _RouterFakeLLM(_verdict("tool_loop", 0.9))
    asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    ))
    assert fake.calls[0]["thinking"] == "low"
    monkeypatch.setenv("AGENT_ROUTER_THINKING", "yolo")
    assert get_router_thinking() == "disabled"          # 非法值回退
    monkeypatch.delenv("AGENT_ROUTER_THINKING", raising=False)
    assert get_router_thinking() == "disabled"          # 未设置 → 默认关思考


# ── 路由分歧仲裁（arbitrate_route，2026-09-18 生产对拍证据定策）───────────────

def _rd(intent: str, rule: str) -> RouteDecision:
    return RouteDecision(intent=intent, matched_rule=rule, slots={}, signals={})


def _ld(intent: str, conf: float) -> RouteDecision:
    decision = RouteDecision(intent=intent, matched_rule="llm-router", slots={}, signals={})
    decision.signals["confidence"] = conf
    return decision


def test_arbitrate_route_policy():
    """仲裁五规则：同向/LLM 缺位 → regex；regex 具体命中 → 不越权；regex 兜底时
    高置信具体意图补位、低置信或兜底型意图不补位（证据见 arbitrate_route docstring）。"""
    regex_specific = _rd(
        "catalog_count", "0.7b:asks_catalog_count+no_resolved+no_brand+no_core_hints"
    )
    regex_fallback = _rd("chitchat", "1:not(has_car_intent or structured)")

    assert arbitrate_route(regex_specific, None) is regex_specific
    assert arbitrate_route(regex_specific, _ld("catalog_count", 0.9)) is regex_specific  # 同向
    assert arbitrate_route(regex_specific, _ld("tool_loop", 0.95)) is regex_specific    # 不越权
    # regex 兜底 + LLM 高置信具体意图 → 补位（known_gap「一共有几款」的修复通道）
    fill = arbitrate_route(regex_fallback, _ld("catalog_count", 0.9))
    assert fill.intent == "catalog_count"
    assert fill.signals["arbitration"] == "fallback_fill"
    assert arbitrate_route(regex_fallback, _ld("chitchat", 0.95)) is regex_fallback
    assert arbitrate_route(regex_fallback, _ld("recommendation", 0.95)) is regex_fallback
    assert arbitrate_route(regex_fallback, _ld("catalog_count", 0.5)) is regex_fallback


def test_route_with_llm_exception_logged_at_info(caplog):
    """P0-2（I-20260918-02）：路由 LLM 异常必须 INFO 可见——无裁决归因的直接证据，
    生产首轮 11 条失败只能靠外部探针反推的补课。"""

    class _ExplodingLLM:
        available = True

        async def chat(self, msgs, **kwargs):
            raise LLMError("DeepSeek API 网络失败：ReadError")

    with caplog.at_level(logging.INFO, logger="app.agent.router"):
        decision = asyncio.run(route_with_llm(
            CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=_ExplodingLLM(),
            regex_intent="catalog_count",
        ))
    assert decision is None
    assert any(
        "调用失败" in rec.getMessage() and "ReadError" in rec.getMessage()
        for rec in caplog.records
    )


def test_shadow_route_retries_then_records(monkeypatch, caplog):
    """P0-1：shadow 旁路异常重试——首试网络失败、次试成功，记录仍落且带仲裁字段。"""
    monkeypatch.setattr("app.agent.engine._SHADOW_RETRY_DELAYS", (0, 0))
    fake = _RouterFakeLLM(_verdict("catalog_count", 0.9), fail_first=1)
    engine = get_agent_engine()
    original = engine._llm
    engine._llm = fake
    regex_decision = _rd("chitchat", "1:not(has_car_intent or structured)")
    try:
        with caplog.at_level(logging.INFO, logger="app.agent.router.shadow"):
            asyncio.run(engine._shadow_route("一共有几款", UserProfile(), regex_decision))
    finally:
        engine._llm = original
    assert fake.router_calls == 2, "首试异常应重试一次"
    records = [
        json.loads(rec.getMessage())
        for rec in caplog.records
        if rec.name == "app.agent.router.shadow"
    ]
    assert records and records[-1]["arbitrated_intent"] == "catalog_count", (
        "regex 兜底 + LLM 高置信计数 → 仲裁补位"
    )
    assert records[-1]["router_version"] == ROUTER_VERSION, "版本标记必须随记录落盘"
    assert records[-1]["router_model"] and records[-1]["router_thinking"]


def test_routing_llm_client_selection(monkeypatch):
    """v2.2：AGENT_ROUTER_MODEL 配置时路由走专用客户端（此前被 self._llm 硬编码旁路）；
    未配置时保持回答链路客户端（既有的测试注入点不变）。"""
    engine = get_agent_engine()
    monkeypatch.delenv("AGENT_ROUTER_MODEL", raising=False)
    assert engine._routing_llm() is engine._llm, "未配置时用回答链客户端（测试注入点）"
    monkeypatch.setenv("AGENT_ROUTER_MODEL", "deepseek-flash")
    reset_router_client()
    try:
        dedicated = engine._routing_llm()
        assert dedicated is _router_client()
        assert dedicated.model == "deepseek-flash"
        assert dedicated is not engine._llm
    finally:
        reset_router_client()  # 不让测试环境变量泄漏进其他测试的客户端单例


def test_shadow_route_retry_exhausted_still_records(monkeypatch, caplog):
    """重试耗尽后仍按 None 落记录（无裁决不丢样本）；旁路尽力而为，绝不影响响应路径。"""
    monkeypatch.setattr("app.agent.engine._SHADOW_RETRY_DELAYS", (0, 0))
    fake = _RouterFakeLLM(None, fail_first=99)
    engine = get_agent_engine()
    original = engine._llm
    engine._llm = fake
    regex_decision = _rd(
        "catalog_count", "0.7b:asks_catalog_count+no_resolved+no_brand+no_core_hints"
    )
    try:
        with caplog.at_level(logging.INFO, logger="app.agent.router.shadow"):
            asyncio.run(engine._shadow_route(CATALOG_ASK, UserProfile(), regex_decision))
    finally:
        engine._llm = original
    assert fake.router_calls == len(_SHADOW_RETRY_DELAYS) + 1, "初试 + 全部重试"
    records = [
        json.loads(rec.getMessage())
        for rec in caplog.records
        if rec.name == "app.agent.router.shadow"
    ]
    assert records and records[-1]["llm_intent"] is None, "重试耗尽 → 无裁决记录"
    assert records[-1]["arbitrated_intent"] == "catalog_count", "无裁决时仲裁维持 regex"


def test_router_client_uses_model_env(monkeypatch):
    """用户决策（2026-09-17）：路由用独立模型档（如 v4.1 flash）——env 即插即用。"""
    monkeypatch.setenv("AGENT_ROUTER_MODEL", "deepseek-v4.1-flash")
    reset_router_client()
    assert _router_client().model == "deepseek-v4.1-flash"
    reset_router_client()
    monkeypatch.delenv("AGENT_ROUTER_MODEL", raising=False)
    from app.common.config import get_settings

    assert _router_client().model == get_settings().deepseek_model, "未设置 → 回答链路同模型"
    reset_router_client()


def test_cache_key_includes_model():
    """缓存 key 含模型名：切换 AGENT_ROUTER_MODEL 不命中旧裁决（评审三轮建议 2）。"""
    assert _cache_key("a", "m1") != _cache_key("a", "m2")
    assert _cache_key(" A  b ", "m1") == _cache_key("a b", "m1")


def test_route_with_llm_non_string_content_and_empty_choices():
    """content 非 str / choices 空：同样必须回退 None（评审三轮探针覆盖的路径）。"""
    fake = _RouterFakeLLM({"choices": [{"message": {"content": 123}}]})
    assert asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake,
    )) is None
    fake2 = _RouterFakeLLM({"choices": []})
    assert asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake2,
    )) is None


def test_llm_intent_executable_preconditions(db_session: Session):
    """评审三轮 B1/B2 的前置条件谓词：各 intent 与 decide_route 守卫逐条一致。"""
    from app.agent.routing import llm_intent_executable

    _seed(db_session)
    empty = UserProfile()
    assert not llm_intent_executable(
        "series_qa", CATALOG_ASK, {}, False, empty, [], db_session
    ), "series_qa 无 resolved 不可执行（修复前会 500）"
    assert not llm_intent_executable(
        "brand_lineup", "奔驰有多少款车", {}, False, empty, [], db_session
    ), "brand_lineup 无品牌不可执行"
    assert not llm_intent_executable(
        "catalog_count", "15万以内有多少款车", {"budget": {"max": 150000}}, True,
        empty, [], db_session,
    ), "带核心约束的计数不可执行（不得答全库数）"
    assert not llm_intent_executable(
        "tool_loop", "今天天气不错", {}, False, empty, [], db_session
    ), "无汽车语境不得启动工具循环"
    assert not llm_intent_executable(
        "tool_loop", "SUV有多少款车", {}, False, empty, [], db_session
    ), "计数问法不得走工具循环（Phase 1 决策：截断结果数总数会编造）"
    assert llm_intent_executable(
        "tool_loop", "解释一下凯美瑞的混动技术", {}, False, empty, [], db_session
    ), "有汽车语境的非计数问法 → tool_loop 可执行"
    assert llm_intent_executable(
        "catalog_count", CATALOG_ASK, {}, False, empty, [], db_session
    ), "无约束的全库计数可执行"
    assert llm_intent_executable("recommendation", CATALOG_ASK, {}, True, empty, [], db_session)
    # 评审四轮：保留的门——否定车系不出档案；已解析车系不被抢成品牌盘点；
    # 计数问法不走工具循环（Phase 1 决策：截断结果数总数会编造）
    from app.common.models import Brand

    toyota_id = db_session.query(Brand).filter_by(name="丰田").first().id
    with_brand = UserProfile()
    with_brand.brand_ids = [toyota_id]
    resolved_kamai = resolve_series(db_session, "我不买凯美瑞了")
    assert resolved_kamai, "种子库应能解析出凯美瑞"
    assert not llm_intent_executable(
        "series_qa", "我不买凯美瑞了", {}, False, empty, resolved_kamai, db_session
    ), "用户刚否定车系时不应出档案（should_answer 门，评审四轮）"
    assert not llm_intent_executable(
        "brand_lineup", "凯美瑞怎么样", {}, False, with_brand, resolved_kamai, db_session
    ), "已解析车系不得被抢成品牌盘点（not resolved 门，评审四轮）"
    assert llm_intent_executable(
        "brand_lineup", "丰田有多少款车", {}, False, with_brand, [], db_session
    ), "品牌 + 盘点/计数问法 → 可执行"
    assert not llm_intent_executable(
        "tool_loop", "盘点一下有多少款车", {}, False, empty, [], db_session
    ), "计数问法不得交给工具循环（截断报数风险，Phase 1 决策）"
    resolved_q = resolve_series(db_session, "凯美瑞的油耗是多少")
    assert llm_intent_executable(
        "series_qa", "凯美瑞的油耗是多少", {}, False, empty, resolved_q, db_session
    ), "正常车系问答可执行"
    # 评审四轮：保留的门——否定车系不出档案；已解析车系不被抢成品牌盘点；
    # 计数问法不走工具循环（Phase 1 决策：截断结果数总数会编造）
    kamaid = resolve_series(db_session, "凯美瑞")
    resolved_kamai = [(kamaid, None)] if kamaid else []
    assert not llm_intent_executable(
        "series_qa", "我不买凯美瑞了", {}, False, empty, resolved_kamai, db_session
    ), "用户刚否定车系时不应出档案（should_answer 门）"
    assert not llm_intent_executable(
        "tool_loop", "盘点一下有多少款车", {}, False, empty, [], db_session
    ), "计数问法不得交给工具循环（截断报数风险，Phase 1 决策）"


def test_route_with_llm_timeout_returns_none():
    """asyncio.wait_for 硬超时：fake 拖 0.4s、限时 30ms → 立即回退，不挂死。"""
    fake = _RouterFakeLLM(_verdict("chitchat", 0.9), router_latency=0.4)
    started = time.perf_counter()
    decision = asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=30, llm=fake, regex_intent="catalog_count",
    ))
    assert decision is None
    assert time.perf_counter() - started < 2.0


def test_route_with_llm_low_confidence_falls_back(monkeypatch):
    monkeypatch.setenv("AGENT_ROUTER_MIN_CONFIDENCE", "0.6")
    fake = _RouterFakeLLM(_verdict("chitchat", 0.3))
    assert asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    )) is None
    # 阈值可用显式参数下调（shadow 对拍即传 0.0 记录原始裁决）
    fake2 = _RouterFakeLLM(_verdict("chitchat", 0.3))
    decision = asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake2, regex_intent="catalog_count",
        min_confidence=0.1,
    ))
    assert decision is not None and decision.intent == "chitchat"


def test_route_with_llm_env_min_confidence(monkeypatch):
    monkeypatch.setenv("AGENT_ROUTER_MIN_CONFIDENCE", "0.2")
    fake = _RouterFakeLLM(_verdict("chitchat", 0.3))
    decision = asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    ))
    assert decision is not None and decision.intent == "chitchat"


def test_route_with_llm_comparison_slots_from_deterministic_extractor():
    """intent==comparison：slots 由确定性提取器从用户原话补齐，不来自 LLM。"""
    message = "（款型ID：11、12、13）帮我分析一下差异"
    fake = _RouterFakeLLM(_verdict("comparison", 0.95))
    decision = asyncio.run(route_with_llm(
        message, UserProfile(), timeout_ms=1000, llm=fake, regex_intent="comparison",
    ))
    assert decision is not None
    assert decision.slots == {"variant_ids": [11, 12, 13]}


def test_route_with_llm_comparison_without_ids_keeps_regex_intent():
    """comparison 提不出 ≥2 个款型 ID：维持 regex intent；未传 regex_intent → None。

    绝不能把缺 variant_ids 的 comparison 决策放进行执行层（slots 直接取键会 KeyError）。
    """
    fake = _RouterFakeLLM(_verdict("comparison", 0.95))
    decision = asyncio.run(route_with_llm(
        "这两台车对比一下", UserProfile(), timeout_ms=1000, llm=fake, regex_intent="tool_loop",
    ))
    assert decision is not None
    assert decision.intent == "tool_loop"
    assert decision.slots == {}
    fake2 = _RouterFakeLLM(_verdict("comparison", 0.95))
    assert asyncio.run(route_with_llm(
        "这两台车对比一下", UserProfile(), timeout_ms=1000, llm=fake2,
    )) is None


def test_route_with_llm_cache_hit_skips_second_llm_call():
    fake = _RouterFakeLLM(_verdict("chitchat", 0.9))
    first = asyncio.run(route_with_llm(
        " 全部车型有多少款车？ ", UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    ))
    assert first is not None and first.intent == "chitchat"
    assert first.signals.get("router_cached") is None
    second = asyncio.run(route_with_llm(
        "全部车型有多少款车？", UserProfile(), timeout_ms=1000, llm=fake, regex_intent="catalog_count",
    ))
    assert second is not None and second.intent == "chitchat"
    assert second.signals["router_cached"] is True
    assert fake.router_calls == 1, "归一化 utterance 精确命中 → 第二次不得再调 LLM"


def test_route_with_llm_unavailable_llm_short_circuits():
    assert asyncio.run(route_with_llm(
        CATALOG_ASK, UserProfile(), timeout_ms=1000, llm=_UnavailableLLM(), regex_intent="catalog_count",
    )) is None


def test_normalize_utterance():
    assert normalize_utterance("  A  B ") == "a b"
    assert normalize_utterance(" 全部车型有多少款车？ ") == normalize_utterance("全部车型有多少款车？")


def test_env_fallbacks(monkeypatch):
    assert get_router_mode() == "regex"                        # 默认
    monkeypatch.setenv("AGENT_ROUTER_MODE", "shadow")
    assert get_router_mode() == "shadow"
    monkeypatch.setenv("AGENT_ROUTER_MODE", "yolo")
    assert get_router_mode() == "regex"                        # 非法值回退
    monkeypatch.setenv("AGENT_ROUTER_TIMEOUT_MS", "250")
    assert get_router_timeout_ms() == 250
    monkeypatch.setenv("AGENT_ROUTER_TIMEOUT_MS", "abc")
    assert get_router_timeout_ms() == 1500
    monkeypatch.setenv("AGENT_ROUTER_MIN_CONFIDENCE", "0.55")
    assert get_router_min_confidence() == pytest.approx(0.55)
    monkeypatch.setenv("AGENT_ROUTER_MIN_CONFIDENCE", "nope")
    assert get_router_min_confidence() == pytest.approx(0.6)


# ── engine 接线（TestClient 端到端）──────────────────────────────────────────

def _swap_llm(fake):
    engine = get_agent_engine()
    original = engine._llm
    engine._llm = fake
    return lambda: setattr(engine, "_llm", original)


def _post(client: TestClient, message: str) -> dict:
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    return client.post(
        f"/api/v1/agent/sessions/{sid}/messages", json={"message": message}
    ).json()


def test_regex_default_mode_never_calls_llm(client: TestClient, db_session: Session):
    """默认模式零新行为：路由不触 LLM，回答走既有确定性 catalog 链路。"""
    _seed(db_session)
    fake = _RouterFakeLLM(_verdict("chitchat", 0.99))
    restore = _swap_llm(fake)
    try:
        out = _post(client, CATALOG_ASK)
    finally:
        restore()
    assert out["filters"].get("catalog_count") is True
    assert fake.calls == [], "regex 默认模式不得发起任何 LLM 调用"


def test_llm_mode_adopts_llm_intent_end_to_end(client: TestClient, db_session: Session, monkeypatch):
    """LLM 路由的价值场景：regex 漏识别（金标 known_gap「一共有几款」）由 LLM 补上，
    且该 intent 在当前上下文可执行（无核心约束/无 resolved/无品牌）→ 采纳并读库报数。"""
    _seed(db_session)
    monkeypatch.setenv("AGENT_ROUTER_MODE", "llm")
    fake = _RouterFakeLLM(_verdict("catalog_count", 0.9))
    restore = _swap_llm(fake)
    try:
        out_llm = _post(client, "一共有几款")
        out_cached = _post(client, "一共有几款")            # 同 utterance 第二次请求
        # 对照组：切回 regex，同一问法回到既有链路（known_gap：不报数）
        monkeypatch.setenv("AGENT_ROUTER_MODE", "regex")
        out_regex = _post(client, "一共有几款")
    finally:
        restore()
    assert out_llm["filters"].get("catalog_count") is True, "LLM 补判的计数应被采纳"
    assert "1 个在售车系" in out_llm["explanation"]
    assert out_regex["filters"].get("catalog_count") is None, "regex 模式维持现状（known_gap）"
    # 缓存：同 utterance 第二次请求不得再触发路由 LLM
    assert out_cached["filters"] == out_llm["filters"]
    assert out_cached["explanation"] == out_llm["explanation"]
    assert fake.router_calls == 1, "同 utterance 第二次路由不得再调 LLM（缓存命中）"


def test_llm_mode_rejects_intent_without_execution_precondition(
    client: TestClient, db_session: Session, monkeypatch
):
    """评审三轮 B1 回归：LLM 判 series_qa 但消息里没有可解析车系（resolved=[]）
    → 不可执行 → 回退 regex 决策；绝不允许 500（修复前 _series_qa_reply IndexError）。"""
    _seed(db_session)
    monkeypatch.setenv("AGENT_ROUTER_MODE", "llm")
    fake = _RouterFakeLLM(_verdict("series_qa", 0.95))
    restore = _swap_llm(fake)
    try:
        out = _post(client, CATALOG_ASK)                    # 消息里没有任何车系名
    finally:
        restore()
    assert out["filters"].get("catalog_count") is True, "应回退 regex 的 catalog_count"
    assert fake.router_calls == 1


def test_llm_mode_respects_core_constraint_guard(client: TestClient, db_session: Session, monkeypatch):
    """评审三轮 B2 回归：带预算约束的计数问句不得被 llm 模式答成全库数
    （修复前「15万以内有多少款车？」被答成全库计数，无视用户约束）。"""
    _seed(db_session)
    monkeypatch.setenv("AGENT_ROUTER_MODE", "llm")
    fake = _RouterFakeLLM(_verdict("catalog_count", 0.95))
    restore = _swap_llm(fake)
    try:
        out = _post(client, "15万以内有多少款车？")
    finally:
        restore()
    assert out["filters"].get("catalog_count") is None, (
        f"带核心约束的计数不得走全库盘点，实际 filters={out['filters']}"
    )
    assert out["need_clarification"] is True, "应回到既有推荐链的追问（Phase 1 语义）"


def test_llm_mode_respects_car_context_guard(client: TestClient, db_session: Session, monkeypatch):
    """评审三轮 B2 回归：无汽车语境的寒暄不得被 llm 模式启动工具循环。"""
    _seed(db_session)
    monkeypatch.setenv("AGENT_ROUTER_MODE", "llm")
    fake = _RouterFakeLLM(_verdict("tool_loop", 0.95))
    restore = _swap_llm(fake)
    try:
        out = _post(client, "今天天气不错")
    finally:
        restore()
    assert out["filters"].get("tool_loop") is None, "汽车语境守卫不得被绕过"
    assert "tool_loop_fallback" not in out["filters"]
    assert out.get("explanation"), "应走既有寒暄/通用对话分支"


def test_llm_mode_falls_back_to_regex_on_bad_output(client: TestClient, db_session: Session, monkeypatch):
    """输出不合法（非法 JSON / intent 越权）→ 立即回退 regex 决策，回答与 regex 一致。"""
    _seed(db_session)
    monkeypatch.setenv("AGENT_ROUTER_MODE", "llm")
    for bad in ("not-json{{{", _verdict("rm_rf_slash", 0.99)):
        clear_route_cache()
        fake = _RouterFakeLLM(bad)
        restore = _swap_llm(fake)
        try:
            out = _post(client, CATALOG_ASK)
        finally:
            restore()
        assert out["filters"].get("catalog_count") is True, f"非法输出必须回退 regex：{bad!r}"
        assert fake.router_calls == 1


def test_llm_mode_falls_back_on_timeout(client: TestClient, db_session: Session, monkeypatch):
    """硬超时（AGENT_ROUTER_TIMEOUT_MS=20 < fake 0.5s）→ 立即回退 regex 决策。"""
    _seed(db_session)
    monkeypatch.setenv("AGENT_ROUTER_MODE", "llm")
    monkeypatch.setenv("AGENT_ROUTER_TIMEOUT_MS", "20")
    fake = _RouterFakeLLM(_verdict("chitchat", 0.9), router_latency=0.5)
    restore = _swap_llm(fake)
    try:
        started = time.perf_counter()
        out = _post(client, CATALOG_ASK)
        wall = time.perf_counter() - started
    finally:
        restore()
    assert out["filters"].get("catalog_count") is True, "超时必须回退 regex 决策"
    assert wall < 2.0, "超时回退应立刻发生，不挂死响应"
    assert fake.router_calls == 1


# ── shadow 模式：零延迟贡献 + 响应一致 + 对拍日志 ─────────────────────────────

def _shadow_records(caplog) -> list[dict]:
    records = []
    for record in caplog.records:
        if record.name == "app.agent.router.shadow":
            try:
                records.append(json.loads(record.getMessage()))
            except ValueError:
                continue
    return records


def _respond_elapsed(caplog, mode: str) -> list[float]:
    """从回答级耗时日志（logger "app.agent.respond"）里取指定路由模式的耗时。"""
    values = []
    for record in caplog.records:
        if record.name == "app.agent.respond":
            try:
                payload = json.loads(record.getMessage())
            except ValueError:
                continue
            if payload.get("mode") == mode:
                values.append(float(payload["elapsed_ms"]))
    return values


def test_shadow_mode_response_identical_and_zero_llm_latency(
    client: TestClient, db_session: Session, monkeypatch, caplog
):
    """shadow 三连断言：

    1) 响应与 regex 模式完全一致（同库同问法：explanation 与 filters 逐字段相等）；
    2) FakeLLM 拖慢 1.5s，响应耗时仍远小于它——LLM 等待绝不在响应路径内；
    3) 旁路完成后对拍记录落 logger "app.agent.router.shadow"，字段齐全。
    """
    _seed(db_session)
    monkeypatch.setenv("AGENT_ROUTER_MODE", "shadow")
    # 超时放宽到 5s：本测试验证「旁路零延迟贡献」，不是超时路径（后者另有专项）——
    # 若用默认 1500ms，fake 的 1.5s 延迟会被硬超时截断，对拍记录变成无裁决
    monkeypatch.setenv("AGENT_ROUTER_TIMEOUT_MS", "5000")
    fake = _RouterFakeLLM(_verdict("chitchat", 0.9), router_latency=1.5)
    restore = _swap_llm(fake)
    try:
        caplog.set_level(logging.INFO)
        started = time.perf_counter()
        out_shadow = _post(client, CATALOG_ASK)
        request_wall = time.perf_counter() - started
        # 对照组：regex 模式（catalog 链路确定性，与路由 LLM 无关）
        monkeypatch.setenv("AGENT_ROUTER_MODE", "regex")
        out_regex = _post(client, CATALOG_ASK)
    finally:
        restore()

    # 1) 响应一致
    assert out_shadow["explanation"] == out_regex["explanation"], "shadow 不得改变回答内容"
    assert out_shadow["filters"] == out_regex["filters"]
    assert out_shadow["filters"].get("catalog_count") is True
    # 2) 零延迟贡献：请求墙钟 + 回答级耗时日志都不含 1.5s 的 LLM 等待
    assert request_wall < 1.2, f"shadow 响应不应等待旁路 LLM（1.5s），实测 {request_wall:.2f}s"
    shadow_elapsed = _respond_elapsed(caplog, "shadow")
    assert shadow_elapsed, "回答级耗时日志应有一条 mode=shadow 记录"
    assert max(shadow_elapsed) < 1500, (
        f"shadow 响应耗时不得包含 LLM 等待（fake 拖慢 1.5s）：{shadow_elapsed}"
    )
    assert _respond_elapsed(caplog, "regex"), "对照组的耗时日志应存在"
    # 3) 对拍记录（旁路在回答产出后才派发——轮询等待；router_calls 断言必须放在
    #    轮询之后：派发点后移使 chat 进入晚于响应返回，旧断言位置会读到 0）
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not _shadow_records(caplog):
        time.sleep(0.05)
    records = _shadow_records(caplog)
    assert records, "shadow 对拍记录应落日志"
    record = records[-1]
    assert record["utterance"] == CATALOG_ASK
    assert record["regex_intent"] == "catalog_count"
    assert record["llm_intent"] == "chitchat"
    assert record["llm_confidence"] == pytest.approx(0.9)
    assert record["agree"] is False
    assert record["llm_elapsed_ms"] >= 1400, "对拍记录应包含真实的 LLM 等待耗时"
    assert fake.router_calls == 1, "shadow 旁路应发起过一次路由 LLM 调用（轮询后断言）"


# ── shadow_report ────────────────────────────────────────────────────────────

_SHADOW_LINES = [
    '{"utterance":"全部车型有多少款车？","regex_intent":"catalog_count","llm_intent":"chitchat",'
    '"llm_confidence":0.9,"agree":false,"llm_elapsed_ms":120.5}',
    '{"utterance":"对比一下秦PLUS和海豹06","regex_intent":"tool_loop","llm_intent":"tool_loop",'
    '"llm_confidence":0.8,"agree":true,"llm_elapsed_ms":300.0}',
    '{"utterance":"你好","regex_intent":"chitchat","llm_intent":null,"llm_confidence":null,'
    '"agree":false,"llm_elapsed_ms":1500.0,"llm_error":"asyncio.TimeoutError"}',
    '{"utterance":"凯美瑞油耗多少","regex_intent":"series_qa","llm_intent":"series_qa",'
    '"llm_confidence":0.7,"agree":true,"llm_elapsed_ms":100.0}',
    "这不是 JSON 的脏行",
    "",
]


def test_shadow_report_aggregate():
    from app.agent.shadow_report import aggregate

    report = aggregate(_SHADOW_LINES)
    assert report["total"] == 4                       # 脏行/空行不计
    assert report["judged"] == 3
    assert report["no_verdict"] == 1                  # 超时单列，不进一致率分母
    assert report["agreed"] == 2
    assert report["agreement_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert report["disagreement_count"] == 1
    assert report["disagreements"][0]["utterance"] == "全部车型有多少款车？"
    assert report["disagreements"][0]["regex_intent"] == "catalog_count"
    assert report["disagreements"][0]["llm_intent"] == "chitchat"
    elapsed = report["llm_elapsed_ms"]
    assert elapsed["count"] == 4                      # 无裁决记录的等待也是真实等待
    assert elapsed["p50"] == pytest.approx(210.25)    # sorted [100, 120.5, 300, 1500] 中位插值
    assert elapsed["p95"] == pytest.approx(1320.0)
    assert report["confusion_pairs"] == [
        {"regex_intent": "catalog_count", "llm_intent": "chitchat", "count": 1}
    ]


def test_shadow_report_parses_prefixed_log_lines():
    """评审三轮 B3 回归：生产日志带 formatter/docker 前缀也必须能解析
    （修复前 `--file shadow.log` 产出空报告且无从分辨）。"""
    from app.agent.shadow_report import aggregate

    prefixed = [
        '2026-09-18 13:39:18,051 INFO app.agent.router.shadow {"utterance":"你好",'
        '"regex_intent":"chitchat","llm_intent":"chitchat","llm_confidence":0.8,'
        '"agree":true,"llm_elapsed_ms":50.0}',
        'backend-1  | 2026-09-18 13:39:19,000 INFO app.agent.router.shadow {"utterance":"你好",'
        '"regex_intent":"chitchat","llm_intent":null,"llm_confidence":null,'
        '"agree":false,"llm_elapsed_ms":1500.0,"llm_error":"asyncio.TimeoutError"}',
        "2026-09-18 13:39:20 INFO app.agent.respond {\"sid\":\"x\",\"mode\":\"shadow\",\"elapsed_ms\":5}",
    ]
    report = aggregate(prefixed)
    assert report["total"] == 2, f"带前缀的真实日志行必须能解析，实际 {report}"
    assert report["judged"] == 1 and report["no_verdict"] == 1
    assert report["agreement_rate"] == pytest.approx(1.0)


def test_shadow_report_markdown_and_cli_roundtrip():
    from app.agent.shadow_report import main as report_main
    from app.agent.shadow_report import aggregate, render_markdown

    markdown = render_markdown(aggregate(_SHADOW_LINES))
    assert "66.67%" in markdown                       # 一致率 2/3
    assert "全部车型有多少款车？" in markdown          # 分歧清单完整可读（供人工标注）
    assert "p50 210.25 ms" in markdown and "p95 1320.0 ms" in markdown

    # CLI：--file 读日志 → --out 写 markdown（backend/eval/ 属 gitignored 本地产物；测试写 tests/.tmp）
    tmp_dir = Path(__file__).resolve().parent / ".tmp"
    src = tmp_dir / "shadow_router_sample.log"
    dst = tmp_dir / "shadow_router_report.md"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    src.write_text("\n".join(_SHADOW_LINES) + "\n", encoding="utf-8", newline="\n")
    assert report_main(["--file", str(src), "--out", str(dst)]) == 0
    assert dst.read_text(encoding="utf-8") == markdown
