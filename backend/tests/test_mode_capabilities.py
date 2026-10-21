"""三档能力表（2026-10-21 结构修复 M2 的回归测试）。

## 之前的问题

三档的语义被拆在**两个布尔门**里：`is_enabled()`（写库并回填）与
`is_observed()`（解析身份）。事故就出在「**选错了门**」：

- 2026-10-21 生产实测：路由层用 `is_enabled()` 决定「是否解析身份」，
  而 `shadow` 档的 `is_enabled()` 为假 ⇒ **影子日志一条都不落**
  ⇒ `shadow` 成了**空档**（不写库、也不观察）
  ⇒ 「先观察一两天再切 on」那道观察期是**假的**。

那次修复的方式是「**再加一个门**」⇒ 选错门的触发面变大而不是变小。

## 现在的形状

`app/agent/preferences.py::ModeCapabilities` + `mode_capabilities()`：
**一张表**说清每档能做什么，调用点按**能力**取（observe / write / refill），
按名字猜不了。

## 本文件钉住的三条

1. 能力表的**内容**与三档的既定语义逐格一致；
2. 未知档位**回退到最保守的那档**（off：什么都不做）；
3. **结构契约**：档位常量与「按档位比较」的写法**只许**出现在
   `preferences.py`（那张表是唯一真值点）——这是本次修复的**核心**，
   没有这条，下一个人又会写 `if get_mode() == MODE_SHADOW:`。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.agent.preferences import (
    MODE_CAPABILITIES,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    mode_capabilities,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = BACKEND_ROOT / "app" / "agent"
PREFS = AGENT_DIR / "preferences.py"


def test_capability_matrix_is_exactly_as_documented():
    """三档 × 三个能力，逐格钉住。

    | 档位 | observe | write | refill |
    | --- | --- | --- | --- |
    | `off` | ✗ | ✗ | ✗ |
    | `shadow` | ✓ | ✗ | ✗ |
    | `on` | ✓ | ✓ | ✓ |
    """
    off = mode_capabilities(MODE_OFF)
    shadow = mode_capabilities(MODE_SHADOW)
    on = mode_capabilities(MODE_ON)

    assert (off.observe, off.write, off.refill) == (False, False, False), off
    assert (shadow.observe, shadow.write, shadow.refill) == (True, False, False), shadow
    assert (on.observe, on.write, on.refill) == (True, True, True), on


def test_shadow_observes_but_never_writes_or_refills():
    """`shadow` 的**全部价值**是观察；它绝不能写库、也绝不能回填。

    ⇒ 这条正是 2026-10-21 那次事故的形状（空档 / 假观察期）的反面。
    """
    shadow = mode_capabilities(MODE_SHADOW)
    assert shadow.observe is True, "shadow 档必须观察（否则观察期是假的）"
    assert shadow.write is False, "shadow 档不得写库"
    assert shadow.refill is False, "shadow 档不得回填（回填会改变推荐结果）"


def test_unknown_mode_falls_back_to_the_most_conservative_caps():
    """未知档位 ⇒ **什么都不做**（安全侧）。"""
    caps = mode_capabilities("garbage-mode")
    assert (caps.observe, caps.write, caps.refill) == (False, False, False), caps
    assert MODE_CAPABILITIES[MODE_OFF] == caps


def test_mode_constants_do_not_leak_outside_the_capability_table():
    """**结构契约**：档位常量只许出现在 `preferences.py`。

    没有这条，下一个人又会写 `if get_mode() == MODE_SHADOW:`——
    而那正是 2026-10-21 事故的形状（语义散落在各处、各处各判一次）。
    """
    offenders: list[str] = []
    for path in sorted((BACKEND_ROOT / "app").rglob("*.py")):
        if path == PREFS:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bMODE_(?:OFF|SHADOW|ON)\b", text):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, f"档位常量泄漏到能力表之外：{offenders}"


def test_nobody_compares_the_mode_by_hand():
    """**结构契约**：`get_mode()` 的结果不得被拿去和档位常量比较。

    「比较档位」＝「按名字猜语义」；语义只许由 `mode_capabilities()` 给。
    """
    offenders: list[str] = []
    pattern = re.compile(r"get_mode\(\)\s*(==|!=|in)|(==|!=)\s*get_mode\(\)")
    for path in sorted((BACKEND_ROOT / "app").rglob("*.py")):
        if path == PREFS:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{lineno}")
    assert not offenders, f"有人在手写「按档位比较」：{offenders}"
