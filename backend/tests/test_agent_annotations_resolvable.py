# -*- coding: utf-8 -*-
"""模块注解里的**未定义名**必须能解析（2026-10-23）

## 背景

`app/agent/turn.py` 的 `advance_profile(..., identity: Any | None = None)` 用了
`typing.Any`，而该模块**没有导入它**。

因为模块里有 `from __future__ import annotations`，注解**不在导入时求值**
⇒ 导入不报错、测试全绿、线上也不炸。

但它是颗定时炸弹：任何**运行时读注解**的东西都会 NameError ——
`typing.get_type_hints()`、pydantic 的 `validate_call`、FastAPI 的依赖注入、
文档生成工具…… 哪天有人用上，错误信息还会指向一个完全无关的地方。

ruff 的 **F821** 能抓到它，但本仓的 ruff `lint` job 是**只报不拦**
⇒ 需要一条**会阻塞**的测试把它变成 CI 里的红灯。
"""
from __future__ import annotations

import inspect
import typing
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
TARGETS = ("turn.py", "engine.py", "preferences.py", "routing.py", "tools.py")


def _module_functions(module_name: str):
    """按模块名导入并列出它的**模块级**函数。"""
    import importlib

    module = importlib.import_module(f"app.agent.{module_name}")
    return [
        (name, obj)
        for name, obj in vars(module).items()
        if inspect.isfunction(obj) and obj.__module__ == module.__name__
    ]


@pytest.mark.parametrize("module_name", TARGETS)
def test_module_annotations_resolve(module_name: str):
    """每个模块级函数的注解都必须**能解析**（`get_type_hints` 不抛 NameError）。

    这条把「注解里的未定义名」从「ruff 只报不拦」变成「CI 会红」。
    """
    broken: list[str] = []
    for name, func in _module_functions(module_name.removesuffix(".py")):
        try:
            typing.get_type_hints(func)
        except NameError as exc:
            broken.append(f"{name}: {exc}")
        except Exception:  # noqa: BLE001 —— 其他解析问题（前向引用等）不归这条管
            continue
    assert not broken, f"{module_name} 的注解里有未定义的名字：{broken}"


def test_turn_advance_profile_identity_annotation_is_resolvable():
    """点名的这一处：`advance_profile` 的 `identity: Any | None` 必须可解析。"""
    from app.agent.turn import advance_profile

    hints = typing.get_type_hints(advance_profile)
    assert "identity" in hints, hints
