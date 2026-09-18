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

from app.agent.engine import get_agent_engine
from app.agent.llm_router import (
    clear_route_cache,
    get_router_min_confidence,
    get_router_mode,
    get_router_timeout_ms,
    normalize_utterance,
    route_with_llm,
)
from app.agent.schemas import UserProfile
from app.common.llm import LLMError
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

# regex 判 catalog_count（金标行）；FakeLLM 改判 chitchat —— 执行分支可从 filters 区分
CATALOG_ASK = "全部车型有多少款车？"


class _RouterFakeLLM:
    """路由测试专用假 LLM（FakeLLM 模式参考 tests/test_tool_loop.py）。

    json_mode=True（路由调用）→ 返回脚本裁决文本（可拖慢 router_latency 秒）；
    其余调用（回答链路）→ 抛 LLMError，让回答落到确定性模板——保证 shadow/llm
    模式下回答内容与「无 LLM 的 regex 模式」可比。
    """

    def __init__(self, verdict: str | None = None, router_latency: float = 0.0):
        self.available = True
        self.verdict = verdict
        self.router_latency = router_latency
        self.calls: list[dict] = []
        self.router_calls = 0

    async def chat(self, msgs, tools=None, temperature=0.2, json_mode=False):
        self.calls.append({"json_mode": json_mode, "n_msgs": len(msgs)})
        if not json_mode:
            raise LLMError("非路由调用：回答链路应回退确定性模板")
        self.router_calls += 1
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
    """「LLM intent ≠ regex intent 且 LLM 合法」：执行走向 LLM 意图分支。

    同一问法「全部车型有多少款车？」：regex 判 catalog_count（读库报数，filters
    带 catalog_count 标记）；FakeLLM 高置信改判 chitchat → 回答变成自然对话分支，
    filters 不再带 catalog_count——执行分支的差异可从响应精确区分。
    """
    _seed(db_session)
    monkeypatch.setenv("AGENT_ROUTER_MODE", "llm")
    fake = _RouterFakeLLM(_verdict("chitchat", 0.9))
    restore = _swap_llm(fake)
    try:
        out_llm = _post(client, CATALOG_ASK)
        out_cached = _post(client, CATALOG_ASK)          # 同 utterance 第二次请求
        # 对照组：切回 regex，同一问法必须走 catalog_count
        monkeypatch.setenv("AGENT_ROUTER_MODE", "regex")
        out_regex = _post(client, CATALOG_ASK)
    finally:
        restore()
    assert "catalog_count" not in out_llm["filters"], "执行应走向 LLM 改判的 chitchat 分支"
    assert out_llm["need_clarification"] is False
    assert out_llm["explanation"], "chitchat 分支应有兜底回复"
    assert out_regex["filters"].get("catalog_count") is True
    assert out_regex["explanation"] != out_llm["explanation"]
    # 缓存：同 utterance 第二次请求不得再触发路由 LLM
    assert out_cached["filters"] == out_llm["filters"]
    assert out_cached["explanation"] == out_llm["explanation"]
    assert fake.router_calls == 1, "同 utterance 第二次路由不得再调 LLM（缓存命中）"


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
    assert fake.router_calls == 1, "shadow 旁路应发起过一次路由 LLM 调用"
    assert _respond_elapsed(caplog, "regex"), "对照组的耗时日志应存在"
    # 3) 对拍记录（旁路 1.5s 后落日志，轮询等待）
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
