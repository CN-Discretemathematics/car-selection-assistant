# -*- coding: utf-8 -*-
"""工具脚本的**导入路径**契约：凡要 import `app.*` 的，就必须先把 `backend/` 放进 sys.path（2026-10-23）

## 这条契约在守什么

`backend/tools/` 下的脚本被**直接执行**时（`python tools/xxx.py`），
`sys.path[0]` 是**脚本自己所在的目录**（`backend/tools/`），**不是 `backend/`**
⇒ `import app.*` 会直接报 `No module named 'app'`。

真实事故：CI 的「migrations + 功能校验（postgres）」job 跑
`python tools/smoke_durable_memory.py --seed-car` ⇒ **自该 job 加入起就没绿过**
⇒ 「真 PostgreSQL 上跑一遍功能校验」那道防线**一直是空的**，而 CI 是红的。

## 判据（按**行为**，不按写法）

模块级 import 了 `app.*` / `tools.*` 的脚本，在**那之前**必须已经：
- import 了 `_bootstrap`（仓库的公共引导，导入即生效），**或**
- 显式 `sys.path.insert/append` 过 `backend/`。

⇒ 两种写法都算过；**只认「在 app import 之前」**——
写在后面等于没写（`import app` 在 import 那一刻就已经失败）。
"""
from __future__ import annotations

import ast
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[1] / "tools").resolve()


def _module_level_imports(tree: ast.Module) -> list[ast.stmt]:
    """模块级的 import 语句与赋值语句（函数体里的不算）。"""
    out: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.Expr)):
            out.append(node)
    return out


def _imports_app_like(node: ast.stmt) -> bool:
    """这条语句是不是在 import `app.*` / `tools.*`。"""
    if isinstance(node, ast.Import):
        return any(
            alias.name == "app" or alias.name.startswith("app.") or alias.name.startswith("tools.")
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        return mod == "app" or mod.startswith("app.") or mod.startswith("tools.")
    return False


def _is_bootstrap_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "") == "_bootstrap"
    if isinstance(node, ast.Import):
        return any(alias.name == "_bootstrap" for alias in node.names)
    return False


def _is_sys_path_fix(node: ast.stmt) -> bool:
    """`sys.path.insert(...)` / `sys.path.append(...)` 形式的路径修补。

    按 **AST 形状**判定，不用字符串匹配——`ast.dump` 里没有 "sys.path" 这个子串
    （它是 `Attribute(value=Name(id='sys'), attr='path')`），字符串匹配会漏。
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
            continue
        func = sub.func
        if (
            func.attr in ("insert", "append")
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            return True
    return False


def test_every_tool_script_that_imports_app_has_backend_on_path():
    """逐个脚本检查：**app 的 import 之前**必须已经把 backend/ 放进 sys.path。"""
    offenders: list[str] = []
    checked = 0
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stmts = _module_level_imports(tree)
        first_app = next((s for s in stmts if _imports_app_like(s)), None)
        if first_app is None:
            continue
        checked += 1
        idx = stmts.index(first_app)
        prelude = stmts[:idx]
        if not any(_is_bootstrap_import(s) or _is_sys_path_fix(s) for s in prelude):
            offenders.append(path.name)
    assert checked >= 10, f"只检查到 {checked} 个脚本，判据可能没生效"
    assert not offenders, (
        "这些脚本在 import app.* 之前没有把 backend/ 放进 sys.path"
        f"（直接执行时会报 No module named 'app'）：{offenders}"
    )


def test_the_ci_postgres_smoke_tool_is_covered():
    """CI 那个 job 用的脚本必须带 bootstrap（防「改回来时漏掉」）。

    job 名与命令见 `.github/workflows/ci.yml` 的「跨会话记忆的端到端功能校验（真 PostgreSQL）」。
    """
    target = TOOLS_DIR / "smoke_durable_memory.py"
    text = target.read_text(encoding="utf-8")
    assert "from _bootstrap import" in text, target.name
    # app 的 import 必须在 bootstrap **之后**
    assert text.index("from _bootstrap import") < text.index("from app.auth.security")
