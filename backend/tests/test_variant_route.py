"""版本问法路由：`asks_variant_diff` 的清点型问法（2026-10-05 端到端扫描发现）。

路由要求「版本类词（HINT）∧ 比较类词（ASK）」**同时**命中。清点型问法
——「Model Y 有哪些版本」「分几个版本」「星愿哪个版本好」「哪款值得买」——
HINT 命中而 ASK 落空，整条确定性版本路径不触发：

    「Model Y有哪些版本」   → HINT ✓ / ASK ✗ → 两条确定性路径都不走 → LLM 链路
    「星愿有几种配置」     → HINT ✗          → 走单车系档案，答定位/核心参数/亮点
                                                ——**一个版本都没提**

只扩 ASK、HINT 不动（仍需「版本/款型/顶配」等显式版本词）。
"""
from __future__ import annotations

import re

import pytest

from app.agent.series_qa import _VARIANT_ASK_RE, asks_variant_diff


def test_ask_regex_has_no_empty_alternative():
    """`_VARIANT_ASK_RE` **不得**含空分支。

    这是本轮挖出的一颗雷：把新增的 ASK 词整段删掉后，正则尾部变成
    `…|值得吗|)`——`|` 与 `)` 之间是**空串**，于是 `re.search` 在位置 0 就命中，
    `asks_variant_diff` 对**任何**含版本词的消息都返回 True，整条版本路径对
    「星愿版本」这类只提版本、不比较的问法也会误触发。
    而当时那批测试**全绿**——因为空分支让一切都为真。
    「变异体没让测试变红」的第三个成因：被测对象退化成了恒真。
    """
    pattern = _VARIANT_ASK_RE.pattern
    assert not re.search(r"\(\s*\|", pattern), f"正则有空分支：{pattern!r}"
    assert not re.search(r"\|\s*\)", pattern), f"正则尾部有悬空 |：{pattern!r}"
    # 直接行为断言：只提版本、不比较 → 不该走版本路径
    assert not asks_variant_diff("星愿版本"), "只提版本未比较却被判为版本问法"
    assert not asks_variant_diff("Model Y版本"), "只提版本未比较却被判为版本问法"


# ── 新覆盖：清点型问法 ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        "Model Y有哪些版本",
        "Model Y有哪几个版本",
        "Model Y分几个版本",
        "星愿哪个版本好",
        "星愿哪款值得买",
    ],
)
def test_counting_phrasings_now_reach_version_path(message: str):
    """改前这 5 条 HINT 命中但 ASK 落空，拿不到版本清单。"""
    assert asks_variant_diff(message), message


# ── 回归：原本就该命中的不能退化 ─────────────────────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        "星愿不同版本有什么区别",
        "星愿各版本差异",
        "星愿版本差异",
        "SU7顶配和低配差在哪",
        "星愿高配和低配区别",
        "星愿版本对比",
        "星愿选哪个版本",
    ],
)
def test_existing_phrasings_still_match(message: str):
    assert asks_variant_diff(message), message


# ── 不得被误判：这些**不是**单车系版本问题 ───────────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        "星愿多少钱",        # 价格
        "星愿续航多少",      # 参数
        "汉L和汉哪个好",    # 两车系对比（引擎会因 len(resolved)>1 不走版本路径）
        "星愿配置怎么样",    # 想问档案，不是版本清单——HINT 认「版本」不认「配置」
        "星愿配置参数",
    ],
)
def test_non_version_phrasings_not_misrouted(message: str):
    """放宽 HINT 会让这批问法被误判；只扩 ASK 正是为了守住这条。"""
    assert not asks_variant_diff(message), message


# ── 显式记录已知未覆盖项（不是缺陷断言，是防漂移的现状说明）──────────────
@pytest.mark.parametrize(
    "message",
    [
        "星愿有几种配置",
        "星愿分几个配置",
        "汉各个配置怎么选",
    ],
)
def test_known_uncovered_counting_phrasings(message: str):
    """已知仍不覆盖：HINT 认「版本」不认「配置」。

    放宽 HINT 的代价是「星愿配置怎么样」这类想问档案的问法被误判，
    用户已于 2026-10-05 在两档方案中选「只补 ASK 词」，故此处钉住现状。

    「汉各个配置怎么选」是本轮**之前就已存在**的漏网（不是本次改动造成），
    一并钉住以免后续误以为它该命中。
    """
    assert not asks_variant_diff(message), message
