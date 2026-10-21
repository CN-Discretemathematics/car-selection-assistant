"""权重知识**单一归属**与**循环依赖消除**的回归测试（2026-10-20）。

背景：权重相关知识此前散在**三个** module——

    app/agent/tools.py    DEFAULT_WEIGHTS / WEIGHT_CEILING / _DIM_LABELS
    app/agent/engine.py   _WEIGHT_RAISE / _USAGE_HINTS / _WEIGHT_DIM_KEYWORDS

于是形成**真实的循环依赖**：

    engine.py (顶层) ──import──▶ soft_prefs.py
                                      │
                                      └──函数内延迟 import──▶ engine.py

Python 不允许顶层互相 import，所以下游只能把 import 写进函数体——
`soft_prefs.py` 与 `soft_probe.py` 里那几处**函数内延迟 import 是疤痕，不是设计**。

团队当时已写下「两处各写一份必然漂移」（soft_prefs.py:723），
却选择用延迟 import 打补丁绕过，而不是收拢 module：
**补丁挡住了崩溃，没挡住漂移。**

本文件钉住两件事：

1. **权重知识只有一份**——`app/agent/weights.py`；
2. **循环依赖不再存在**——下游不再需要函数内延迟 import `engine`。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent import engine, profile, soft_prefs, soft_probe, tools, turn, weights

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"


# ── 1. 权重知识单一归属 ────────────────────────────────────────────────────


def test_weights_module_holds_all_weight_knowledge():
    """8 维定义、默认权重、上限、增量、关键词、用途词表**全在 weights.py**。"""
    for name in (
        "DIM_LABELS",
        "DEFAULT_WEIGHTS",
        "WEIGHT_CEILING",
        "WEIGHT_RAISE",
        "WEIGHT_DIM_KEYWORDS",
        "USAGE_HINTS",
    ):
        assert hasattr(weights, name), f"weights.py 缺少 {name}"


def test_consumers_reference_the_same_objects():
    """各消费方拿到的必须是**同一个对象**，不是各自的一份拷贝。

    这是「知识有归属」的可执行判据：拷贝就意味着两份，会漂移。

    2026-10-20：`engine` 不再是权重的消费方——`extract_hints` 与画像解析
    一并搬进了 `app/agent/turn.py`，engine 只剩**展示**用的 `_WEIGHT_RAISE`
    等名字的 re-export。故此处只断言 engine **仍然持有**的那些。
    """
    assert tools.DEFAULT_WEIGHTS is weights.DEFAULT_WEIGHTS
    assert tools.WEIGHT_CEILING is weights.WEIGHT_CEILING
    assert tools._DIM_LABELS is weights.DIM_LABELS
    assert engine._WEIGHT_RAISE == weights.WEIGHT_RAISE
    assert engine._USAGE_HINTS is weights.USAGE_HINTS
    assert soft_prefs.DEFAULT_WEIGHTS is weights.DEFAULT_WEIGHTS
    assert soft_prefs.WEIGHT_RAISE == weights.WEIGHT_RAISE
    # 画像解析那一侧（turn）也必须拿同一份
    assert turn.DEFAULT_WEIGHTS is weights.DEFAULT_WEIGHTS
    assert turn.WEIGHT_RAISE == weights.WEIGHT_RAISE
    assert turn.USAGE_HINTS is weights.USAGE_HINTS
    assert turn.WEIGHT_DIM_KEYWORDS is weights.WEIGHT_DIM_KEYWORDS


def test_weight_values_are_pinned():
    """把 8 维的**具体数值**钉住。

    「同一对象」只能防住「各写一份」；防不住**有人改了唯一那一份**——
    而后者恰恰是更要紧的：权重直接决定排序，8 维默认值 + 增量 + 上限
    是三处协同的口径（默认 → 加强调增量 → 封顶）。

    这些数字各自有来历，不是随手写的：
    - `budget` 0.30 最高（预算是硬约束之外的**第一**软偏好）
    - `usage` 0.15 —— 8 维里**唯独没有关键词入口**的维度却给到 0.15，
      因为它是「像不像这台车会被这样用」的总闸
    - `comfort` / `intelligence` 0.05 最低（配置类事实最不决定「值不值得买」）
    - `WEIGHT_RAISE` 0.2 —— 必须**大于** 最高默认权重与次高之差，
      否则「强调某维」反而把该维压低（2026-10-03 实测的「越强调越不看重」）
    - `WEIGHT_CEILING` 1.0 —— 封顶后「一句车叠到 3.0」不可能
    """
    assert weights.DEFAULT_WEIGHTS == {
        "budget": 0.30,
        "usage": 0.15,
        "space": 0.10,
        "energy": 0.15,
        "power": 0.10,
        "comfort": 0.05,
        "intelligence": 0.05,
        "maintenance": 0.10,
    }
    assert weights.WEIGHT_CEILING == 1.0
    assert weights.WEIGHT_RAISE == 0.2
    # 维度名与 8 维权重键必须**一一对应**——多一个少一个都会让
    # 「真被测量的维度」这个集合与打分公式脱节
    assert set(weights.DIM_LABELS) == set(weights.DEFAULT_WEIGHTS)

def test_weight_raise_does_not_push_a_dimension_below_its_default():
    """「强调某维」不得把该维**压低**。

    profile.weights 是**绝对权重（覆盖）**，所以强调时发的是
    「默认值 + 增量」。若增量小于「默认值 − 次高值」，就会出现
    「用户越强调价格，系统越不看重价格」——2026-10-03 实测踩过。
    """
    defaults = sorted(weights.DEFAULT_WEIGHTS.values(), reverse=True)
    gap = defaults[0] - defaults[1]
    assert gap <= weights.WEIGHT_RAISE, (
        f"WEIGHT_RAISE={weights.WEIGHT_RAISE} 小于最高与次高默认权重之差 {gap}——"
        f"强调次高维度会把它压到默认权重以下"
    )
    # 且任何维度加上增量后都不得超过上限
    for dim, base in weights.DEFAULT_WEIGHTS.items():
        assert base + weights.WEIGHT_RAISE <= weights.WEIGHT_CEILING, dim


def test_usage_keywords_are_measurable():
    """`WEIGHT_DIM_KEYWORDS` 里映射到 usage 的词，必须也在 `USAGE_HINTS` 里。

    否则该词会让 tools 侧把 usage 计入 measured 集合，而画像里并没有
    usage ——**加权却测不到 = 白白稀释其它维度而换不来任何排序变化**。
    该不变量此前由 tests/test_agent.py 钉住，此处再钉一次是因为词表搬了家。
    """
    usage_keywords = {kw for kw, dim in weights.WEIGHT_DIM_KEYWORDS if dim == "usage"}
    assert usage_keywords, "usage 维度不该没有关键词入口"
    assert usage_keywords <= set(weights.USAGE_HINTS), (
        f"这些 usage 关键词不在 USAGE_HINTS 里："
        f"{sorted(usage_keywords - set(weights.USAGE_HINTS))}"
    )


def test_merge_profile_is_one_function():
    """画像合并器全仓只有一处，且各消费方拿到的是**同一个函数对象**。"""
    assert engine.merge_profile is profile.merge_profile
    assert soft_prefs.merge_profile is profile.merge_profile
    assert soft_probe.merge_profile is profile.merge_profile


# ── 2. 循环依赖不再存在 ────────────────────────────────────────────────────


def _function_level_imports(path: Path) -> list[str]:
    """返回该文件里**函数体内**的 `from app.agent... import ...` 语句。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module and sub.module.startswith("app.agent"):
                    found.append(f"{sub.module}:{','.join(a.name for a in sub.names)}")
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    Visitor().visit(tree)
    return found


@pytest.mark.parametrize("module_name", ["soft_prefs", "soft_probe"])
def test_downstream_modules_do_not_import_engine_at_function_level(module_name: str):
    """`soft_prefs` / `soft_probe` 不得在**函数体内** import `engine`。

    这就是那条循环依赖的疤痕本体。顶层 import 也不行——engine 顶层 import
    soft_prefs，所以任何方向的 engine import 都会成环。
    """
    hits = [
        h
        for h in _function_level_imports(AGENT_DIR / f"{module_name}.py")
        if h.startswith("app.agent.engine")
    ]
    assert not hits, f"{module_name}.py 仍在函数体内 import engine: {hits}"


def test_no_module_imports_soft_prefs_and_soft_probe_at_function_level():
    """反向也查一遍：engine 顶层 import soft_prefs/soft_probe，
    所以它们**任何位置**都不该再 import engine（含顶层）。
    """
    for module_name in ("soft_prefs", "soft_probe"):
        source = (AGENT_DIR / f"{module_name}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.agent.engine":
                pytest.fail(
                    f"{module_name}.py 仍 import app.agent.engine（行 {node.lineno}）——"
                    f"engine 顶层 import {module_name}，这会成环"
                )


def test_weights_module_has_no_agent_imports():
    """`weights.py` 必须是**叶子**：不 import 任何 app.agent.* 模块。

    它是所有消费方的公共底座，一旦它反向依赖谁，新环立刻形成。
    """
    tree = ast.parse((AGENT_DIR / "weights.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            pytest.fail(f"weights.py 不应 import {node.module}（行 {node.lineno}）——它必须是叶子")


def test_profile_module_has_no_agent_imports():
    """`profile.py` 同样是叶子：只依赖 schemas 与 weights。"""
    tree = ast.parse((AGENT_DIR / "profile.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.agent."):
            allowed = {"app.agent.schemas", "app.agent.weights"}
            assert node.module in allowed, (
                f"profile.py 不应 import {node.module}（行 {node.lineno}）"
            )
