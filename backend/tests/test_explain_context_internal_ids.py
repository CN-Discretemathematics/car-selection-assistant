"""解释模块的上下文里**不能有内部 id**（2026-10-21）。

## 为什么

可追问率那笔的附带发现：用户锁定某车系而候选为空时，模型在回答里写了
「并锁定了**车系 ID 905**」——

- **泄漏内部 id**：用户看不懂「905」是什么；
- **问不出处**：画像里确实有这个数，但它是**主键**不是事实——
  「这个 905 来自哪条记录」没有答案。

根因：解释模块的上下文直接用了 `profile.model_dump()`，
把 `locked_series_ids` / `brand_ids` 这些**只对内部有意义**的字段一起给了模型。

## 守卫

1. `llm_profile_view` 去掉内部 id 字段，**保留可读同义字段**（品牌名等）；
2. 解释模块的上下文里确实没有内部 id（用捕获上下文的假 LLM 断言）；
3. **落盘与内部记账不受影响**——id 字段还在画像里（锁定/去重靠它们）。
"""
from __future__ import annotations

import asyncio
import json

from app.agent.engine import AgentEngine
from app.agent.schemas import UserProfile, llm_profile_view
from app.agent.session import SessionStore


class _CapturingLLM:
    """捕获发给模型的上下文的假 LLM。"""

    available = True

    def __init__(self) -> None:
        self.context = ""

    async def chat(self, messages, temperature: float = 0.0, **kwargs):
        self.context = messages[1]["content"]
        return {"choices": [{"message": {"content": "这是一段解释性回答。"}}]}


def _profile() -> UserProfile:
    return UserProfile(
        locked_series_ids=[905],
        last_recommended_variant_ids=[1234],
        brand_ids=[7],
        brand_labels=["测试品牌"],
        brand_exclude_ids=[8],
        brand_exclude_labels=["排除品牌"],
    )


def test_llm_profile_view_drops_internal_ids():
    """视图里没有内部 id 字段，但**可读字段还在**。"""
    view = llm_profile_view(_profile())
    for field in (
        "locked_series_ids",
        "last_recommended_variant_ids",
        "brand_ids",
        "brand_exclude_ids",
    ):
        assert field not in view, view
    assert view["brand_labels"] == ["测试品牌"], view
    assert view["brand_exclude_labels"] == ["排除品牌"], view


def test_explain_context_has_no_internal_ids():
    """解释模块的上下文里不能出现内部 id。"""
    llm = _CapturingLLM()
    engine = AgentEngine(llm=llm, store=SessionStore())
    result = {
        "variants": [
            {
                "variant_id": 1,
                "series_id": 5,
                "series_name": "甲车系",
                "brand_name": "测试品牌",
                "display_name": "甲车系 2026款",
                "energy_type": "BEV",
                "price_cny": 150000.0,
                "score": 0.91,
                "matched": ["预算匹配"],
                "tradeoffs": [],
                "source_id": 1,
            }
        ],
        "count": 1,
    }
    asyncio.run(engine._explain(_profile(), result, [], []))
    context = json.loads(llm.context)
    for field in ("locked_series_ids", "last_recommended_variant_ids", "brand_ids"):
        assert field not in context["profile"], context["profile"]
    assert "905" not in llm.context, llm.context


def test_profile_storage_still_keeps_internal_ids():
    """**落盘与内部记账不受影响**——锁定/去重靠的就是这些 id。"""
    profile = _profile()
    stored = profile.model_dump()
    assert stored["locked_series_ids"] == [905], stored
    assert stored["last_recommended_variant_ids"] == [1234], stored
    assert stored["brand_ids"] == [7], stored
