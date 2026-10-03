"""画像落盘：把「不丢画像」钉成结构性保证（2026-10-02 P5.4 / M4）。

`respond()` 原本在同一轮里**连续**写 3 次 profile——品牌合并后、解锁后、重新锁定后——
写的是同一个对象，后一次必然覆盖前一次。生产存储是云 Redis（socket_timeout=3s），
每次都是一次串行往返。已合并为 1 次无条件写，落盘点 8 → 6。

剩余 6 处**各有不可替代的职责**，本测试逐条钉住；合并的真正风险不是多写，
是某条分支漏写（表现为「追问之后下一轮失忆」）：

  932   is_chatty 早退之前——那时品牌/锁定逻辑尚未执行，这次写是唯一落盘机会
  995   吸收品牌合并 / 解锁 / 重新锁定三种变更（**无条件**）
  1116  定向追问 return 分支
  1120  温和引导 return 分支
  1127  追加 unknowns 后 emit 追问的 return 分支
  1165  自动解锁之后（解锁发生在 995 之后，1165 落的才是解锁后的画像）
"""
from __future__ import annotations

import ast
from pathlib import Path

import app.agent.engine as engine_mod


def _fn(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(Path(engine_mod.__file__).read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return n  # type: ignore[return-value]
    raise AssertionError(f"未找到函数 {name}")


def _writes(fn: ast.AST) -> list[int]:
    """落盘行号。

    注意实际写法是 `run_in_threadpool(self._store.set_profile, ...)`——
    `self._store.set_profile` 是**被当作参数传入的 Attribute 节点**，
    按 `Call` 去找一个都找不到（这坑我踩过一次）。
    """
    return sorted(
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr == "set_profile"
    )


def _is_guarded(fn: ast.AST, line: int) -> bool:
    """该行是否位于 If/For/While/Try 之内。"""
    for parent in ast.walk(fn):
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.Try)):
                if any(
                    isinstance(g, ast.Attribute) and g.attr == "set_profile" and g.lineno == line
                    for g in ast.walk(child)
                ):
                    return True
    return False


def test_post_mutation_write_is_unconditional():
    """吸收三处变更的那次写**必须在函数体顶层**。

    若它被条件包住，「锁了车系但没给预算」这类组合就会丢画像——下一轮失忆，
    且症状（"它忘了我刚说的"）与病因（少了一次 Redis 写）相距极远，极难排查。
    """
    lines = _writes(_fn("respond"))
    assert lines, "respond() 一次都不落盘"
    post_mutation = lines[1]  # 932 是早退前的，995 才是吸收变更的
    assert not _is_guarded(_fn("respond"), post_mutation), (
        f"行 {post_mutation} 的落盘被包在条件里了"
    )


def test_early_return_write_precedes_every_return():
    """is_chatty 早退路径的落盘必须早于所有 return。"""
    fn = _fn("respond")
    lines = _writes(fn)
    first = lines[0]
    earliest_return = min(
        (n.lineno for n in ast.walk(fn) if isinstance(n, ast.Return)), default=None
    )
    assert earliest_return is not None, "respond() 没有 return？"
    assert first < earliest_return, (
        f"第一次落盘（行 {first}）晚于最早的 return（行 {earliest_return}）"
    )


def test_exit_path_writes_are_preserved():
    """三条退路分支各自的落盘不得合并。"""
    lines = _writes(_fn("respond"))
    assert len(lines) >= 5, f"退路分支上的落盘被误删：{lines}"


def test_no_two_sequential_writes_in_mutation_block():
    """回归本轮修的那处：品牌/解锁/重锁三处**相邻**写不得再出现。

    旧结构是 960 / 980 / 987 三个彼此相隔几行的写（写同一对象，纯冗余）。
    现在该区域只剩一次无条件写。

    ## 为什么不再用行号窗口（2026-10-03 改）

    原先断言写死了行号窗口 `936 <= ln <= 1010`。于是**在上方任何一次编辑**
    （哪怕只是加几行注释或一个辅助函数）都会把窗口挪空、报 `0 != 1`——
    一个与被测性质完全无关的假红灯。否定句重写在 `extract_hints` 上方加了约
    110 行，`respond()` 整体下移，原窗口当场失效。

    改为按**函数自身结构**判定，与行号彻底解耦：整段 `respond()` 里
    **无条件**（不在 if/for/while/try 内）的落盘必须恰好是两处——早退前那次与
    吸收变更后那次；其余每一处都在 return 分支里。比原来更强：原断言只检查
    「某个魔法窗口里有 1 次写」，现在检查的是「全局只有 2 次无条件写」。
    """
    fn = _fn("respond")
    writes = _writes(fn)
    assert len(writes) >= 5, f"退路分支上的落盘数量异常：{writes}"
    unconditional = [ln for ln in writes if not _is_guarded(fn, ln)]
    assert unconditional == writes[:2], (
        f"无条件落盘应恰好是前两处（早退前 + 吸收变更后），实际 {unconditional}；"
        f"多出来的即为本测试回归的冗余写，全部落盘：{writes}"
    )


def test_unlock_write_survives():
    """自动解锁发生在 995 之后，1165 落的才是解锁后的画像——不得删。"""
    lines = _writes(_fn("respond"))
    assert len(lines) >= 5 and lines[-1] > lines[1], (
        f"解锁后的落盘（应晚于吸收变更那次）不见了：{lines}"
    )
