# -*- coding: utf-8 -*-
"""日志可观测性测试（W0-P0 收尾，评审二轮阻塞项回归）。

评审实测：仓库此前没有任何 logging 配置，uvicorn 默认配置下 `app.agent.routing`
的 INFO 路由决策日志被静默丢弃——「路由可观测」在生产不成立。本文件钉住：
1. configure_logging() 后 root 有 handler、routing logger 的 INFO 可达（不被丢弃）；
2. 配置幂等（重复调用不叠加 handler）；
3. log_route_decision 的 utterance 做 PII 掩码（手机号/邮箱/长数字串不原文落盘）。
"""
from __future__ import annotations

import json
import logging

from app.agent.routing import RouteDecision, log_route_decision
from app.common.logging_setup import configure_logging


def test_configure_logging_makes_routing_info_visible():
    """评审二轮阻塞项回归：配置后 routing 的 INFO 必须可达 root handler（不静默丢弃）。"""
    configure_logging()
    root = logging.getLogger()
    assert root.handlers, "root 必须有 handler，否则 INFO 被静默丢弃"
    logger = logging.getLogger("app.agent.routing")
    assert logger.getEffectiveLevel() <= logging.INFO, "routing logger 的 INFO 必须可达"

    # 真发一条：INFO 记录必须能到达 root 上的某个 handler（而不是被有效级别挡掉）
    seen: list[logging.LogRecord] = []

    class _Probe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    probe = _Probe()
    root.addHandler(probe)
    try:
        logger.info("route-decision-probe-line")
    finally:
        root.removeHandler(probe)
    assert any("route-decision-probe-line" in r.getMessage() for r in seen), (
        "INFO 记录必须能穿过有效级别到达 handler"
    )


def test_configure_logging_is_idempotent():
    configure_logging()
    count = len(logging.getLogger().handlers)
    configure_logging()
    configure_logging()
    assert len(logging.getLogger().handlers) == count, "重复配置不得叠加 handler"


def test_log_route_decision_masks_pii(caplog):
    """utterance 是用户原话：手机号/邮箱/长数字串必须先掩码再落日志。"""
    message = "我手机 13812345678，邮箱 a.b@c.com，证件 110101199001011234，想看SUV"
    with caplog.at_level("INFO", logger="app.agent.routing"):
        log_route_decision(
            "session-pii-check",
            message,
            RouteDecision("catalog_count", "0.7b:asks_catalog_count"),
            elapsed_ms=1.0,
        )
    line = caplog.records[-1].getMessage()
    payload = json.loads(line)
    utterance = payload["utterance"]
    assert "13812345678" not in utterance, "手机号不得原文落日志"
    assert "a.b@c.com" not in utterance, "邮箱不得原文落日志"
    assert "110101199001011234" not in utterance, "证件号不得原文落日志"
    assert "***" in utterance, f"应出现掩码标记，实际：{utterance}"
    assert "想看SUV" in utterance, "非 PII 部分应保留（掩码不是整句丢弃）"
