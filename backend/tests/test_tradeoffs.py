"""取舍叙事的汇总与**上限**（2026-10-02 修正 + 抽取）。

原实现在 `respond()` 内联，且上限判断与 `break` 都放在**内层**循环里：

    for v in top:
        for t in v["tradeoffs"] or []:
            ...
            if len(tradeoffs) >= 4:
                break          # <- 只跳出内层，外层继续

于是「最多 4 条」实际上限是 `4 + (候选车系数 - 1)`：每多一个车系就可能多塞一条。

该 bug 此前不可达——`recommendation_tool` 从不往 `tradeoffs` 里 append
（见 docs/refactoring-roadmap.md P4.6）。但取舍叙事是 sales-agent 提案里
投入产出比最高的一步（L3 修活 tradeoffs 链），一旦启用就会立刻显形，
故先修正并抽成可测函数。

**本测试的真正价值**：把「上限」这个此前无人验证、也无人能验证的约定钉死。
"""
from __future__ import annotations

from app.agent.engine import collect_tradeoffs


def _candidates(*groups: list[str]) -> list[dict]:
    return [{"tradeoffs": list(g)} for g in groups]


def test_limit_is_strict_across_candidates():
    """核心回归：上限必须在**候选之间**也成立（原实现做不到）。"""
    # 3 个车系各 3 条，期望总数仍为 4；原实现会给出 6。
    top = _candidates(["a1", "a2", "a3"], ["b1", "b2", "b3"], ["c1", "c2", "c3"])
    out = collect_tradeoffs(top, limit=4)
    assert len(out) == 4, f"上限 4 被突破，实际 {len(out)} 条：{out}"
    assert out == ["a1", "a2", "a3", "b1"]


def test_limit_one_and_zero():
    assert collect_tradeoffs(_candidates(["a", "b"]), limit=1) == ["a"]
    assert collect_tradeoffs(_candidates(["a", "b"]), limit=0) == []


def test_under_limit_passes_through_in_order():
    top = _candidates(["a1", "a2"], ["b1"])
    assert collect_tradeoffs(top, limit=4) == ["a1", "a2", "b1"]


def test_deduplicates_across_candidates():
    top = _candidates(["x", "y"], ["x", "z"])
    assert collect_tradeoffs(top, limit=10) == ["x", "y", "z"]


def test_internal_notes_are_filtered():
    """内部说明类措辞不呈现给用户（评审要求）。"""
    top = _candidates(
        ["未参与评分", "价格未披露", "暂无数据源"],
        ["后排空间小", "数据源待补"],
    )
    out = collect_tradeoffs(top, limit=10)
    assert "后排空间小" in out
    for internal in ("未参与评分", "价格未披露", "暂无数据源", "数据源待补"):
        assert internal not in out


def test_empty_and_missing_tradeoffs_are_safe():
    assert collect_tradeoffs([]) == []
    assert collect_tradeoffs([{}, {"tradeoffs": None}, {"tradeoffs": []}]) == []


def test_current_state_is_dead_chain():
    """记录现状：recommendation_tool 恒产出空 tradeoffs，故本函数目前恒返回 []。

    这不是断言 bug，而是**钉住现状**——等 L3 启用取舍叙事时，这条会先失败，
    提醒执行者「链路已接通，记得把上限那条也一起验」。
    """
    from app.agent.tools import recommendation_tool  # noqa: F401  仅确认可导入

    assert collect_tradeoffs([{"tradeoffs": []}], limit=4) == []
