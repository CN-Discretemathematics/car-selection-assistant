# -*- coding: utf-8 -*-
"""差异分析的 LLM 一句话点评（grounded，宁缺毋滥）。

口径（与 Agent 的确定性纪律一致，见 skills/sku-comparison.md）：
- 只允许复述确定性分析给出的事实（verdict / summary / tradeoffs），由 prompt 显式给定；
- 点评里**不得出现任何数字**——数字结论已由确定性层呈现，LLM 层只做自然语言归纳；
  出现任何数字即视为失控、直接丢弃（gap 百分比是派生值，数字白名单对其不完备，
  故这里用比白名单更严的「零数字」约束）；
- LLM 未配置 / 超时 / 校验失败 → 返回 None，前端回退确定性 verdict——宁可没有点评，不可编造；
- 结果按款型组合缓存（LLM 调用有成本，同组款型反复切换不重复计费）。
"""
from __future__ import annotations

import asyncio
import re

from app.common.llm import LLMError, get_llm_client

_MAX_CACHE = 128
_MAX_CHARS = 100
_TIMEOUT_SECONDS = 12.0
_CACHE: dict[tuple[int, ...], str] = {}


def build_comment_messages(analysis: dict) -> list[dict[str, str]] | None:
    """从确定性分析构造受限 prompt；没有可复述的事实时返回 None。"""
    facts = [
        analysis.get("verdict"),
        *(analysis.get("summary") or [])[:6],
        *(analysis.get("tradeoffs") or []),
    ]
    facts = [f.strip() for f in facts if f and f.strip()]
    if not facts:
        return None
    system = (
        "你是家用新车选购助手。只允许依据「已核实事实」写一句中文点评；"
        "不得出现任何数字（一个都不行）；不得提及优惠、库存、成交价、贷款或任何链接；"
        "不超过 60 字；直接输出点评正文，不要任何前缀或引号。"
    )
    user = (
        "已核实事实：\n" + "\n".join(f"- {f}" for f in facts)
        + "\n请写一句自然的点评（不超过 60 字，不得出现数字）。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def ai_comment_for(analysis: dict) -> str | None:
    """生成 LLM 点评；任何失败/越界都返回 None（调用方回退确定性 verdict）。"""
    key = tuple(sorted(v["variant_id"] for v in analysis.get("variants", [])))
    if not key:
        return None
    if key in _CACHE:
        return _CACHE[key]
    messages = build_comment_messages(analysis)
    if messages is None:
        return None
    client = get_llm_client()
    if not client.available:
        return None
    try:
        resp = await asyncio.wait_for(client.chat(messages, temperature=0.4), timeout=_TIMEOUT_SECONDS)
        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        text = re.sub(r"\s+", "", text).strip()
        # 超长 = 跑题；含数字 = 失控（确定性层已负责数字）——一律丢弃
        if not text or len(text) > _MAX_CHARS or re.search(r"\d", text):
            return None
    except (LLMError, asyncio.TimeoutError, OSError, KeyError, TypeError, IndexError, ValueError):
        return None
    if len(_CACHE) >= _MAX_CACHE:
        _CACHE.clear()
    _CACHE[key] = text
    return text
