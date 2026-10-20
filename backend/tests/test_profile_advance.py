"""画像推进**单一归属**的回归测试（2026-10-20）。

背景：`engine.respond()` 的开头是**与 LLM 无关的确定性画像推进**，
却与 LLM 调用路径交织在一个函数里：

    段一 · 画像合并   载入 → hints → L1 → unknowns → L4 → 落盘①
    闲聊早退
    段二 · 车系锁定   车系解析 → 品牌约束 → 锁定状态机 → 对比跟随 → 落盘②

于是「画像怎么推进」这件事**没有任何 interface**——调用者要知道的不是
「怎么用这个函数」，而是「这 110 行的顺序不能动」。最直接的证据是
engine.py 里那行注释：

> ⚠️ 必须在 set_profile **之前**：放后面的话权重改了却没落盘，下一轮又变回原样，
> 表现为「用户点了没反应」

**顺序正确性只能靠注释和纪律维护，没有 compile-time 保护。**

现在整段收进 `app/agent/turn.py`：
- `advance_profile`      段一：载入 → 合并 → L1 → unknowns → L4
- `advance_series_locks` 段二：车系解析 → 品牌 → 锁定状态机 → 对比跟随

本文件钉住：
1. engine.py 里**不再有**画像推进的直接赋值（AST 级）；
2. 段一在闲聊时**仍然执行**、段二**不执行**（边界不能被合并掉）；
3. 落盘次数与位置不变。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.agent import turn
from app.agent.schemas import UserProfile

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
ENGINE_PY = AGENT_DIR / "engine.py"
TURN_PY = AGENT_DIR / "turn.py"


def _assigned_profile_fields(path: Path) -> list[tuple[int, str]]:
    """该文件里所有 `profile.<字段> = ...` 的直接赋值。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "profile"
            ):
                out.append((node.lineno, t.attr))
    return out


# ── 1. 画像推进已移出 engine.py ────────────────────────────────────────────


def test_engine_no_longer_assigns_profile_fields():
    """engine.py 里不该再有 `profile.<字段> = ...` 的**画像推进**赋值。

    剩下的两处是**分发期**逻辑，不是推进：
    - `locked_series_ids`：锁定车系与本轮硬约束冲突时自动解锁
    - `last_recommended_variant_ids`：推荐后记录

    故本断言按**字段**白名单，而不是「一处都不许有」——
    否则将来新增分发期字段会被误判。
    """
    fields = {attr for _ln, attr in _assigned_profile_fields(ENGINE_PY)}
    assert fields <= {"locked_series_ids", "last_recommended_variant_ids"}, (
        f"engine.py 里出现了新的画像直接赋值：{sorted(fields - {'locked_series_ids', 'last_recommended_variant_ids'})}"
    )


def test_unknowns_cleanup_moved_out_of_engine():
    """`unknowns` 的清理（budget/usage/passengers）整体搬进了 turn.py。

    它是段一的组成部分——「最近一次追问未答」的语义必须与画像合并同处。

    判据是**赋值**而非「出现」：engine.py 里仍要**读** `profile.unknowns`
    （分发期决定要不要追问），读是合理的，写才该搬走。
    """
    engine_fields = {attr for _ln, attr in _assigned_profile_fields(ENGINE_PY)}
    assert "unknowns" not in engine_fields, (
        "engine.py 里还有 profile.unknowns 的赋值——应已随画像推进搬进 turn.py"
    )
    turn_src = TURN_PY.read_text(encoding="utf-8")
    assert "profile.unknowns" in turn_src, "turn.py 里应保留 unknowns 清理"


def test_turn_module_exposes_the_two_stages():
    """两个阶段都是公开入口——调用方不该再自己拼顺序。"""
    assert callable(turn.advance_profile)
    assert callable(turn.advance_series_locks)


# ── 2. 两段的边界不能被合并掉 ──────────────────────────────────────────────


def test_extract_hints_lives_with_the_profile_advance():
    """`extract_hints`（画像解析）与 `advance_profile`（画像推进）同域。"""
    assert hasattr(turn, "extract_hints")
    assert "def extract_hints" in TURN_PY.read_text(encoding="utf-8")
    assert "def extract_hints" not in ENGINE_PY.read_text(encoding="utf-8"), (
        "extract_hints 仍留在 engine.py——它与画像推进同域，应同住 turn.py"
    )


def test_turn_profile_carries_the_three_outputs():
    """`TurnProfile` 携带 profile / hints / structured 三样，调用方不必再推导。"""
    tp = turn.TurnProfile(profile=UserProfile(), hints={"usage": ["通勤"]}, structured=True)
    assert tp.profile.budget.min is None
    assert tp.hints == {"usage": ["通勤"]}
    assert tp.structured is True


# ── 3. 落盘次数与位置不变 ──────────────────────────────────────────────────


def test_set_profile_call_count_unchanged():
    """`set_profile` 的调用次数不变（收拢只是搬家，不是省一次写）。

    改前：respond 里 7 处调用（1233/1319/1447/1451/1458/1496/1608）。
    改后：段一后 1 处 + 段二后 1 处 + 其余分发期的 5 处 = 同样 7 处。
    少一次就意味着「某条分支的画像变更没落盘」——那是静默丢数据。
    """
    src = ENGINE_PY.read_text(encoding="utf-8")
    count = src.count("self._store.set_profile")
    assert count == 7, f"set_profile 调用数从 7 变成 {count}——画像可能没落盘"


def test_profile_advance_does_not_write_to_store():
    """`advance_profile` / `advance_series_locks` **不落盘**。

    落盘点依赖早退分支（闲聊早退在段一后、版本差异早退在段二后），
    若 module 自己落盘，早退分支就得知道「谁落了盘」，interface 会变宽。
    """
    src = TURN_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "set_profile":
                # 只允许 _load_profile 的**脏数据自愈**那一次
                parent_ok = False
                for n in ast.walk(tree):
                    if (
                        isinstance(n, ast.AsyncFunctionDef)
                        and n.name == "_load_profile"
                        and n.lineno <= node.lineno <= (n.end_lineno or 0)
                    ):
                        parent_ok = True
                        break
                assert parent_ok, (
                    f"turn.py L{node.lineno} 有 set_profile 调用——"
                    f"只有 _load_profile 的脏数据自愈允许落盘"
                )
