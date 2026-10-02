"""取舍叙事（tradeoffs）的**反编造**保证（2026-10-02 修活 L3）。

取舍叙事说「这台的好处是 X，代价是 Y」。代价必须**相对同批候选**才有意义。
本测试锁住两条最容易被绕过的编造路径：

1. **不得用「缺数据」当劣势**。space/power 的 0.5、comfort/intelligence 的 0.0
   表示「库内没有这项数据」，不是「这台更差」。若拿它去和别的候选比，
   就等于**用缺失数据编造一个负面事实**——正是本项目设计原则第 1 条禁止的。
2. **不得因噪声报取舍**。落后 0.01 不是取舍，写出来只稀释真信息。
"""
from __future__ import annotations

from app.agent.tools import _DIM_LABELS, _TRADEOFF_GAP


def test_gap_threshold_is_not_noise():
    """低于阈值的落后不报取舍。"""
    assert _TRADEOFF_GAP >= 0.2, "阈值过低会把 0.0x 的噪声写进用户可见文案"


def test_every_label_has_a_dimension_and_vice_versa():
    """标签表与实际评分维度一一对应，缺一个就少一类取舍。

    DEFAULT_WEIGHTS 是 `recommendation_tool` 的**函数内**局部量（profile 可覆盖），
    不可从模块导入，故这里用源码里的权重键字面量作对照。
    """
    from pathlib import Path

    import app.agent.tools as tools_mod

    src = Path(tools_mod.__file__).read_text(encoding="utf-8")
    start = src.index("DEFAULT_WEIGHTS = {")
    block = src[start : src.index("}", start)]
    weight_dims = {
        ln.split(":")[0].strip().strip('"')
        for ln in block.splitlines()[1:]
        if ":" in ln
    }
    assert set(_DIM_LABELS) == weight_dims, (
        f"标签与权重维度不一致："
        f"仅标签 {set(_DIM_LABELS) - weight_dims}，"
        f"仅权重 {weight_dims - set(_DIM_LABELS)}"
    )


def test_no_numbers_in_labels():
    """文案不含数字：回答契约要求正文数字必须可溯源，引入分数会把它搞复杂。"""
    for dim, label in _DIM_LABELS.items():
        assert not any(ch.isdigit() for ch in label), f"{dim} 的标签含数字：{label}"


def test_missing_data_never_becomes_a_tradeoff():
    """核心断言：缺数据（0.5 中性 / 0.0）**不得**进入 measured。

    实现上 measured 只在该维度有库内实值时才被加入；本测试用两个构造好的
    scored 记录验证取舍判定对「无数据」与「有数据但落后」的处理不同。
    """
    from app.agent.tools import _tradeoff_gaps  # 会在下一处定义

    best = {"space": 1.0, "power": 1.0}
    # 本候选：space 无数据（未列入 measured），power 有数据但落后
    dims = {"space": 0.5, "power": 0.4}
    measured = {"power"}
    gaps = _tradeoff_gaps(dims, measured, best)
    assert any("动力" in g for g in gaps), f"有真实数据的落后应被报出：{gaps}"
    assert not any("空间" in g for g in gaps), f"缺数据不得被当成劣势：{gaps}"


def test_uniform_scores_produce_no_tradeoffs():
    """全批打平时不该有任何取舍——否则每台都会被无差别贴标签。"""
    from app.agent.tools import _tradeoff_gaps

    best = {"space": 0.8}
    assert _tradeoff_gaps({"space": 0.8}, {"space"}, best) == []
