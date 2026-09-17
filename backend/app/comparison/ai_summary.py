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


def _facts_text(analysis: dict) -> list[str]:
    """可复述的确定性事实（verdict + summary 头几条 + 取舍句）。"""
    facts = [
        analysis.get("verdict"),
        *(analysis.get("summary") or [])[:6],
        *(analysis.get("tradeoffs") or []),
    ]
    return [f.strip() for f in facts if f and f.strip()]


def build_comment_messages(analysis: dict) -> list[dict[str, str]] | None:
    """从确定性分析构造受限 prompt；没有可复述的事实时返回 None。"""
    facts = _facts_text(analysis)
    if not facts:
        return None
    system = (
        "你是家用新车选购助手。只允许依据「已核实事实」写一句中文点评；"
        "只能使用事实里已经出现过的数字，一个都不许新增或换算（没把握就不写数字）；"
        "不得提及优惠、库存、成交价、贷款或任何链接；"
        "不超过 60 字；直接输出点评正文，不要任何前缀或引号。"
    )
    user = (
        "已核实事实：\n" + "\n".join(f"- {f}" for f in facts)
        + "\n请写一句自然的点评（不超过 60 字；数字只能用上面出现过的）。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _allowed_numbers(analysis: dict) -> set[float]:
    """点评允许出现的数字 = 事实文本里出现过的数字（含差距百分比、万元价差）。

    比「零数字」宽松但同样防编造：模型只能复述事实里的数字，凭空换算/新增会被拦下。
    """
    pool: set[float] = set()
    for text in _facts_text(analysis):
        for token in re.findall(r"\d+(?:\.\d+)?", text):
            pool.add(round(float(token), 3))
    for value in analysis.get("allowed_numbers") or []:
        pool.add(round(float(value), 3))
    return pool


def _numbers_within(text: str, allowed: set[float]) -> bool:
    """点评里的每个数字都必须能在事实数字集合里找到（容差 0.01，与 Agent 同一口径）。"""
    for token in re.findall(r"\d+(?:\.\d+)?", text):
        value = float(token)
        if not any(abs(value - candidate) < 0.01 for candidate in allowed):
            return False
    return True


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
    allowed_numbers = _allowed_numbers(analysis)
    try:
        resp = await asyncio.wait_for(client.chat(messages, temperature=0.4), timeout=_TIMEOUT_SECONDS)
        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        text = re.sub(r"\s+", "", text).strip()
        # 超长 = 跑题；出现事实之外的数字 = 编造 —— 一律丢弃并回退确定性结论
        if not text or len(text) > _MAX_CHARS or not _numbers_within(text, allowed_numbers):
            return None
    except (LLMError, asyncio.TimeoutError, OSError, KeyError, TypeError, IndexError, ValueError):
        return None
    if len(_CACHE) >= _MAX_CACHE:
        _CACHE.clear()
    _CACHE[key] = text
    return text
