"""记忆边际价值评估工具的判定逻辑（2026-10-21）

工具本体：`backend/tools/eval_memory_value.py`。

## 为什么这几行要单测

这个工具的**结论**全靠 `classify()` 三个分支。若它把「关掉记忆也全过」误判成
「记忆有效」，报告就会把**恒真的断言**当成证据——那正是本仓反复踩的坑
（记忆删除场景曾经「记忆功能整体关闭时照样 50/50 通过 ⇒ 恒真」）。
"""
from __future__ import annotations

from tools.eval_memory_value import MEMORY_SCENARIOS, MEMORY_TYPE, classify


def test_off_group_all_pass_is_not_a_control() -> None:
    """关掉记忆也 100% 通过 ⇒ 断言恒真 ⇒ **不构成对照**（不能算「记忆有效」）。"""
    verdict = classify(on_rate=1.0, off_rate=1.0)
    assert "不构成对照" in verdict
    assert "有效" not in verdict.replace("不构成对照", "")


def test_memory_effective_when_off_group_fails() -> None:
    """off 组红、on 组绿 ⇒ 记忆有效。"""
    assert classify(on_rate=1.0, off_rate=0.0) == "记忆有效"
    assert classify(on_rate=0.8, off_rate=0.2) == "记忆有效"


def test_no_improvement_is_reported_as_such() -> None:
    """记忆开着也没比关着好 ⇒ 「记忆未带来改善」，**不能**说成「有效」。"""
    assert classify(on_rate=0.5, off_rate=0.5) == "记忆未带来改善"
    assert classify(on_rate=0.3, off_rate=0.6) == "记忆未带来改善"


def test_every_memory_scenario_has_a_label() -> None:
    """新增跨会话场景类型时**必须**给出记忆类型标签。

    否则报告里那一行会退化成场景 id，看报告的人不知道它检验的是哪类记忆——
    「同一件事写多遍必然漂移」的一个小变体。
    """
    missing = [s for s in MEMORY_SCENARIOS if s not in MEMORY_TYPE]
    assert not missing, f"这些场景类型没有记忆类型标签：{missing}"
