"""会话历史注入 prompt 时的**预算**（2026-10-21，修 P4 缺陷台账 M 项）。

## 为什么需要

会话存储最多留 `MAX_MESSAGES = 50` 条消息、每条最多 2000 字（接口上限）
→ 最坏 **100K 字**直接进 prompt。中文 1 字 ≈ 1 token，64K 上下文的模型会被
**直接拒收**（context length exceeded）→ 用户看到的是降级回答，而不是他的问题。

而且这份历史是**每轮重发**的：长对话的每一次提问都带着整段历史，
成本与延迟随轮数线性涨。

## 口径（三条，都写明以便推翻）

1. **预算单位是字符，不是 token**。本仓没有 DeepSeek 的分词器，而字符是 token 的
   **保守**代理（中文 1 字 ≈ 1 token、英文 1 字 ≈ 0.25 token）→
   按字符卡只会偏严、不会偏松 ✓。
2. **保留最近的对话**（近因更重要），从旧到新丢弃。
3. **丢弃必须明说**：调用方在 system 提示里留一行「此前 N 条对话未提供」，
   并记一条日志——**静默截断会让模型以为自己看到了全部对话**，
   然后据此编造「你刚才说过预算 15 万」。

## 为什么独立成 module

engine.py 已经 2400 行；2026-10-20 的架构收拢方向是**把知识从 engine 里搬出去**
（`weights.py` / `profile.py` / `turn.py`）。本 module 是纯计算、无状态、不碰 db，
符合那个方向 ✓。
"""
from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("app.agent.prompt")

#: 历史注入 prompt 的字符预算。
#:
#: 取 8000 的理由：正常对话（50 条 × 30 字 ≈ 1500 字）**远低于**它 → 常见场景
#: 行为逐位不变；只有「长对话 + 长消息」才会触发截断。
#: 而 8000 字 ≈ 8000 token（中文），加上 system 提示与数据佐证，总 prompt 仍在
#: 64K 上下文的 1/4 以内 ✓。
HISTORY_CHAR_BUDGET = 8000


def select_history(
    history: list[dict[str, Any]], *, budget: int = HISTORY_CHAR_BUDGET
) -> tuple[list[dict[str, Any]], int]:
    """从**最近**往回挑消息，直到字符预算用完。

    返回 `(入选消息, 被省略条数)`。

    规则：

    - **至少留一条**：即使单条就超预算，也保留它（否则完全无上下文更糟）；
    - **掐在一轮中间要修**：入选的第一条若是 `assistant`（半轮对话），
      丢掉开头几条直到第一条是 `user`——半轮对话某些后端会拒收；
    - **不改动**消息内容本身（本 module 只**选**，不**改**）。
    """
    kept: list[dict[str, Any]] = []
    total = 0
    for msg in reversed(history):
        size = len(msg.get("content") or "")
        if kept and total + size > budget:
            break
        kept.append(msg)
        total += size
    kept.reverse()

    # 半轮对话：开头不是 user 就往后丢
    while kept and kept[0].get("role") != "user":
        kept.pop(0)

    dropped = len(history) - len(kept)
    if dropped:
        _logger.info(
            "会话历史超出预算（%d 字）：省略 %d 条，保留最近 %d 条",
            budget, dropped, len(kept),
        )
    return kept, dropped


def truncation_note(dropped: int) -> str:
    """被省略时的提示语（放进 system 提示，让模型知道上下文不完整）。"""
    if dropped <= 0:
        return ""
    return f"（此前 {dropped} 条对话因长度限制未提供。）"
