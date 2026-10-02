"""shadow_report 分歧表渲染的转义与版本兼容（2026-10-03 由 ruff 首次实跑引出）。

起因：`render_markdown` 的分歧表原先把 `.replace('|', '\\\\|')` **内联在 f-string 里**。
f-string 内允许反斜杠转义是 PEP 701，**Python 3.12 才合法**。本仓对外声明支持
3.11+（README 徽章 / `requires-python = ">=3.11"`），而 CI 与生产镜像都是 3.12，
所以 3.11 部署会在 import 这个模块时直接 SyntaxError——**没有任何自动化会撞到它**。

这类缺陷 ruff 的解析阶段就能抓到（`invalid-syntax`），但前提是 ruff 真的被执行过。
本文件的作用有二：

1. 钉住转义行为：`| ` 必须转义成 `\\|`，否则会把 Markdown 表格撑破（多出列），
   渲染出来的报告在 GitHub 上是错位的——而 shadow 报告正是用来做切流判读的。
2. 让这段代码**以后再也不能**用 3.12-only 语法：用一个按 ruff 判定的测试守住
   （不能用 `ast.parse(feature_version=...)`，原因见该测试内注释——那是假绿灯）。
   这是本仓对 `requires-python` 承诺的自动化执行点之一，权威检测点是 CI 的 lint job。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.agent import shadow_report
from app.agent.shadow_report import render_markdown

_ROW_RE = re.compile(r"^\| \d+ \| ")


def _disagreement_rows(report: dict) -> list[str]:
    """从渲染结果里取出分歧表的数据行（表头是「utterance」，分隔行是全 ---）。"""
    return [ln for ln in render_markdown(report).splitlines() if _ROW_RE.match(ln)]


def test_pipe_in_utterance_is_escaped():
    """用户原话里带 `|` 时必须转义，否则表格多出一列、报告错位。"""
    rows = _disagreement_rows({
        "disagreements": [{
            "utterance": "A|B",
            "regex_intent": "recommendation",
            "llm_intent": "series_qa",
            "llm_confidence": 0.9,
        }]
    })
    assert len(rows) == 1
    assert "A\\|B" in rows[0]
    # 去掉内容里的转义管道符后，行内应恰好剩 6 个分隔符 = 6 列。
    # （直接数 rows[0].count("|") 会把 A\|B 里那个也算进去，得 7——那不是列数。）
    assert rows[0].replace("\\|", "").count("|") == 6


def test_missing_and_falsy_fields_render_as_empty_not_crash():
    """缺 utterance / confidence 为 None 都要照常出行，不能抛异常。"""
    rows = _disagreement_rows({
        "disagreements": [
            {"regex_intent": "x", "llm_intent": "y", "llm_confidence": 0.1},
            {"utterance": "", "regex_intent": "x", "llm_intent": "y", "llm_confidence": None},
        ]
    })
    assert len(rows) == 2
    assert rows[0].startswith("| 1 |  | x | y | 0.1 |")
    assert rows[1].startswith("| 2 |  | x | y | None |")


def test_backslash_and_pipe_combined():
    """原话本身带反斜杠时不能被误当成转义序列（只替换管道符，不动其它）。"""
    rows = _disagreement_rows({
        "disagreements": [{
            "utterance": "带反斜杠\\和|竖线",
            "regex_intent": "x",
            "llm_intent": "y",
            "llm_confidence": 0.2,
        }]
    })
    assert "带反斜杠\\和\\|竖线" in rows[0]


def test_module_parses_under_python_311_grammar():
    """`requires-python = ">=3.11"` 是对外承诺，必须由 CI 自动核验而非靠人记得。

    ⚠️ 这里**不能**用 `ast.parse(src, feature_version=(3, 11))`：我第一版就是这么写的，
    实测它是**假绿灯**——把 3.12-only 的内联转义改回去，测试照样 4 passed。
    原因是 PEP 701 属**词法层**变更（f-string 内的反斜杠），CPython 的
    `feature_version` 只门控 match/walrus 那一类语法特性，管不到 f-string 内部。
    「写了个看起来在守门的东西，却从没验证过它真的会响」——这正是本仓在治的病，
    自己不能犯。

    真正能抓的是 ruff：它按 `target-version = "py311"` 解析，会直接报
    `invalid-syntax: Cannot use an escape sequence ... on Python 3.11`。
    因此权威检测点是 CI 的 **lint** job（2026-10-03 接入），本测试是它的重复保险。

    ruff 未安装时跳过，不让一个可选工具变成测试失败原因——但那也意味着
    **lint job 才是真正的门禁**，本测试不构成独立保障。
    """
    import subprocess
    import sys

    probe = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        import pytest

        pytest.skip("ruff 未安装：3.11 语法的权威检测点是 CI 的 lint job")

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(Path(shadow_report.__file__)),
         "--select", "E", "--output-format", "concise"],
        capture_output=True,
        text=True,
    )
    assert "invalid-syntax" not in result.stdout, (
        "shadow_report 用了 3.12-only 语法（f-string 内反斜杠转义），"
        "在 3.11 上会 SyntaxError，而本仓声明支持 3.11+：\n" + result.stdout
    )
