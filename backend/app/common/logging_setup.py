# -*- coding: utf-8 -*-
"""进程级日志配置（一次性、幂等）。

背景（2026-09-17 评审二轮阻塞项）：仓库此前没有任何 logging 配置，uvicorn 默认配置下
`app.agent.routing` 的 INFO 路由决策日志被静默丢弃——W0-P0「路由可观测」在生产不成立
（pytest 里能看到只是因为 caplog 挂了 handler）。

在应用导入时（app.main）调用一次：root logger 挂 handler 并设级别（LOG_LEVEL 环境变量
控制，默认 INFO）；app.* logger 默认向 root 传播，无需逐个挂 handler。幂等：root 已有
handler（如 pytest 注入）时只对齐级别，不重复挂。
"""
from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        root.setLevel(level)
