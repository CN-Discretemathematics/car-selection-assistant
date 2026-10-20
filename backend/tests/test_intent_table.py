"""意图清单**单一归属**的回归测试（2026-10-20）。

背景：「意图」此前有**三份清单**，语义各不相同，名字却都像「意图」：

    routing.INTENTS          8 个   LLM 输出**协议白名单**（llm_router 用它拒非法输出）
    llm_router._LLM_FILL_INTENTS  5 个   LLM 允许**补位**的具体意图
    （无）                       9 个   decide_route 实际产出的全部意图

`sales_ranking` 是第 9 个——它由纯确定性规则判定，**LLM 连输出都不许**，
所以它不在 `INTENTS` 里，**这不是漏列**。

真正的毛病是**名字骗人 + 完整集合无处可查**：一个叫 `INTENTS` 的常量只管
LLM 那一半，而 `RouteDecision` 的 docstring 写着「命中的链路（INTENTS 之一）」
——那句话是错的。新读代码的人会以为 `INTENTS` 就是全部意图。

现在用一张表（`routing.INTENT_SPECS`）说清每个 intent 在各路径下的角色，
并派生 `INTENTS` / `LLM_FILL_INTENTS` / `ALL_INTENTS`。

本文件钉住：
1. 表是唯一来源（两个派生清单都由它生成）；
2. **顺序不变**——`INTENTS` 的顺序直接决定 LLM 提示词里的枚举顺序；
3. 全部意图有声明，且 `decide_route` 产出的每一个都在 `ALL_INTENTS` 里。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.agent import llm_router
from app.agent.routing import ALL_INTENTS, INTENT_SPECS, INTENTS, LLM_FILL_INTENTS

ROUTING_PY = Path(__file__).resolve().parents[1] / "app" / "agent" / "routing.py"
LLM_ROUTER_PY = Path(__file__).resolve().parents[1] / "app" / "agent" / "llm_router.py"


# ── 1. 表是唯一来源 ────────────────────────────────────────────────────────


def test_derived_constants_come_from_the_table():
    """两个清单必须**由表派生**，不是各自维护。"""
    assert tuple(s.name for s in INTENT_SPECS if s.llm_protocol) == INTENTS
    assert frozenset(s.name for s in INTENT_SPECS if s.llm_fill) == LLM_FILL_INTENTS
    assert tuple(s.name for s in INTENT_SPECS) == ALL_INTENTS


def test_llm_router_uses_the_derived_fill_set():
    """`llm_router` 不得再自己硬写一份「LLM 可补位」清单。"""
    assert llm_router._LLM_FILL_INTENTS is LLM_FILL_INTENTS
    source = LLM_ROUTER_PY.read_text(encoding="utf-8")
    # 只查代码（不查注释）——注释里提到旧实现是文档
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"frozenset\(\s*\{[^}]*catalog_count", code), (
        "llm_router 又硬写了一份可补位清单——应从 routing.INTENT_SPECS 派生"
    )


def test_all_intents_are_declared():
    """`ALL_INTENTS` 必须包含全部 9 个意图，含纯确定性的 `sales_ranking`。"""
    assert len(ALL_INTENTS) == 9
    assert "sales_ranking" in ALL_INTENTS
    assert set(ALL_INTENTS) == {s.name for s in INTENT_SPECS}


# ── 2. 顺序不能变（影响 LLM 提示词枚举顺序）────────────────────────────────


def test_intents_order_unchanged():
    """`INTENTS` 的**顺序**直接进 LLM 提示词（`llm_router` 逐行拼 `- {name}`）。

    改了顺序 = 改了提示词 = 可能改 LLM 输出分布。故把迁移前的原始顺序钉住。
    """
    assert INTENTS == (
        "comparison",
        "series_qa",
        "brand_lineup",
        "catalog_count",
        "tool_loop",
        "chitchat",
        "general_advice",
        "recommendation",
    )


def test_llm_protocol_set_unchanged():
    """LLM 协议白名单的**内容**不变——`sales_ranking` 仍不在其中。"""
    assert set(INTENTS) == {
        "comparison",
        "series_qa",
        "brand_lineup",
        "catalog_count",
        "tool_loop",
        "chitchat",
        "general_advice",
        "recommendation",
    }
    assert "sales_ranking" not in INTENTS


def test_llm_fill_set_unchanged():
    """LLM 可补位集合不变：5 个具体意图，兜底型（chitchat/general_advice/
    recommendation）不在其中——用它们改写 regex 的具体规则命中等于换一个兜底。
    """
    assert frozenset(
        {"catalog_count", "series_qa", "brand_lineup", "tool_loop", "comparison"}
    ) == LLM_FILL_INTENTS


# ── 3. 表与代码实际行为一致 ────────────────────────────────────────────────


def test_decide_route_intents_are_subset_of_all_intents():
    """`decide_route` 里出现的每个 `intent="..."` 都必须在 `ALL_INTENTS` 里。

    这是「表不漏」的判据：给 `decide_route` 加了新意图却忘了加表，
    这条会变红。
    """
    source = ROUTING_PY.read_text(encoding="utf-8")
    produced = set(re.findall(r'intent="([a-z_]+)"', source))
    assert produced, "没找到 intent= 字面量——判定方式失效了？"
    assert produced <= set(ALL_INTENTS), f"这些意图没在表里声明：{produced - set(ALL_INTENTS)}"
    # 且表里不应有多余的（声明了但没人产出）
    assert set(ALL_INTENTS) == produced, f"表里这些意图没人产出：{set(ALL_INTENTS) - produced}"


def test_route_decision_docstring_mentions_all_intents():
    """`RouteDecision` 的 docstring 不得再说「INTENTS 之一」。

    那句话是错的——`INTENTS` 只有 8 个，而 `decide_route` 产出 9 个。
    文档与实现漂移会让下一个读代码的人以为 `INTENTS` 就是全部意图。
    """
    tree = ast.parse(ROUTING_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RouteDecision":
            doc = ast.get_docstring(node) or ""
            assert "INTENTS 之一" not in doc, "RouteDecision docstring 仍说「INTENTS 之一」"
            assert "ALL_INTENTS" in doc, "RouteDecision docstring 应指向 ALL_INTENTS"
            return
    pytest.fail("找不到 RouteDecision 类")


def test_every_intent_has_consistent_roles():
    """每个 intent 的两个角色标记必须自洽。

    - `llm_fill=True` 蕴含 `llm_protocol=True`（能补位的前提是 LLM 能输出它）；
    - 兜底型（chitchat/general_advice/recommendation）不可补位。
    """
    fallback = {"chitchat", "general_advice", "recommendation"}
    for spec in INTENT_SPECS:
        if spec.llm_fill:
            assert spec.llm_protocol, f"{spec.name}: 能补位就必须能输出"
        if spec.name in fallback:
            assert not spec.llm_fill, f"{spec.name}: 兜底型意图不许补位"
