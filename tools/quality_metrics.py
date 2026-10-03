#!/usr/bin/env python
"""代码质量指标看板（8 维，纯标准库 AST，零新增依赖）。

存在理由
--------
本仓长期只有「自研静态门禁」（密钥/文案/文档），**没有任何通用静态分析**：
没有 ruff、没有 mypy、没有复杂度度量、没有覆盖率。557 个用例能守住行为，
但对「结构正在腐化」这件事毫无发言权——`engine.py` 涨到 1968 行、某函数复杂度
飙到 261，测试全绿也照样通过。

本脚本把 `docs/engineering-standards.md` §4.2 定义的 8 维指标变成**可复算的
报告**，供 CI 归档为 artifact。看板存在 = 坏味道没有被忽略。

设计约束
--------
1. **零第三方依赖**：只用 `ast`。CI 的 lint job 不必先装 ruff 就能产出看板。
2. **只报不拦**（默认）：存量超标是既成事实，一次性拦截会把 CI 立刻打红。
   需要拦截时显式传 `--fail-on <维度>=<阈值>`。
3. **假阳性必须显式排除**：
   - 长参数列表：FastAPI 路由由 `Query()/Path()/Depends()` 生成的参数不算
     （`sales/router.py:home()` 11 个、`vehicles/router.py:vehicle_list()` 10 个
     都是框架强制，不是 smell）。
   - 重复率：3 行访问器不计（抽象收益低于成本，会淹没真正值得修的项）。
4. **认知复杂度与圈复杂度必须成对读**。两者排序不同：`_build_questions` 认知
   270 全仓第一、圈复杂度只第 6（单点深嵌套所致，拆平即消失）；
   `analyze_comparison` 两项都最高（真正需重构）。单看一个会排错优先级。

用法
----
    python tools/quality_metrics.py                 # 人读报告
    python tools/quality_metrics.py --json          # 机读（CI artifact）
    python tools/quality_metrics.py --fail-on cc-cognitive=300
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 阈值（docs/engineering-standards.md §4.2）────────────────────────────────
THRESHOLDS = {
    "cc-cognitive": 15,   # 新增函数上限；存量超标走递减冻结
    "cc-mccabe": 10,
    "func-lines": 80,
    "file-lines": 600,
    "nesting": 4,
    "max-args": 5,
    "duplication": 0.05,
    "fan-out": 10,
}

# 复杂度算子（与 SonarSource 认知复杂度定义对齐的简化版：
# 嵌套加权 + 布尔序列计 n-1 + 推导式计一次）
_NESTING_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While,
    ast.ExceptHandler, ast.IfExp, ast.Assert, ast.comprehension,
)


def cognitive_complexity(fn: ast.AST) -> int:
    def walk(node: ast.AST, depth: int) -> int:
        total = 0
        for child in ast.iter_child_nodes(node):
            cls = type(child)
            if cls in _NESTING_NODES:
                total += 1 + depth
                total += walk(child, depth + 1)
            elif cls is ast.BoolOp:
                n = len(child.values) - 1
                total += (n if n > 0 else 1)
                total += walk(child, depth)
            else:
                total += walk(child, depth)
        return total

    return walk(fn, 0)


def mccabe(fn: ast.AST) -> int:
    c = 1
    for n in ast.walk(fn):
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While,
                          ast.ExceptHandler, ast.IfExp, ast.Assert, ast.With,
                          ast.AsyncWith)):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            c += 1 + len(n.ifs)
    return c


def max_nesting(fn: ast.AST) -> int:
    best = 0

    def walk(node: ast.AST, depth: int) -> None:
        nonlocal best
        inc = isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While,
                                ast.Try, ast.With, ast.AsyncWith))
        nd = depth + 1 if inc else depth
        best = max(best, nd)
        for c in ast.iter_child_nodes(node):
            walk(c, nd)

    for stmt in getattr(fn, "body", []):
        walk(stmt, 0)
    return best


def arg_count(fn: ast.AST) -> int:
    a = fn.args
    n = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
    return n + (1 if a.vararg else 0) + (1 if a.kwarg else 0)


def is_fastapi_route(fn: ast.AST) -> bool:
    """路由处理器的 Query()/Path()/Depends() 参数是框架强制的，不计长参数。"""
    for d in getattr(fn, "decorator_list", []):
        for sub in ast.walk(d):
            if isinstance(sub, ast.Attribute) and sub.attr in {
                "get", "post", "put", "delete", "patch", "websocket",
            }:
                return True
    return False


def tracked(rel_prefixes: tuple[str, ...]) -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", *rel_prefixes],
        capture_output=True, text=True, cwd=ROOT,
    )
    if proc.returncode != 0:
        raise SystemExit("git ls-files 失败：无法取得跟踪文件清单")
    return [ROOT / p for p in proc.stdout.split("\0") if p]


def collect_python() -> tuple[list[dict], dict[str, set[str]], dict[str, int]]:
    """返回 (函数指标, 模块依赖, 文件行数)。排除测试（测试本身不受本看板约束）。"""
    files = [p for p in tracked(("backend/app", "backend/tools")) if p.suffix == ".py"]
    funcs: list[dict] = []
    imports: dict[str, set[str]] = collections.defaultdict(set)
    file_lines: dict[str, int] = {}

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        file_lines[rel] = len(src.splitlines())

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("app."):
                        imports[rel].add(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                imports[rel].add(node.module)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.append({
                    "file": rel,
                    "line": node.lineno,
                    "name": node.name,
                    "lines": node.end_lineno - node.lineno + 1,
                    "cc-cognitive": cognitive_complexity(node),
                    "cc-mccabe": mccabe(node),
                    "nesting": max_nesting(node),
                    # 路由处理器豁免：否则 FastAPI 强制参数全部误判
                    "max-args": 0 if is_fastapi_route(node) else arg_count(node),
                })
    return funcs, imports, file_lines


_COMMENT = re.compile(r"#.*$")
_STRING = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.S)


def collect_duplication(win: int = 5) -> dict:
    """归一化行窗口重复率。排除 import 头与短行。"""
    exts = ("backend/app", "backend/tools", "web/app", "web/lib")
    files = [p for p in tracked(exts) if p.suffix in {".py", ".ts", ".tsx"}]
    seen: dict[int, list[tuple[str, int]]] = collections.defaultdict(list)
    total = 0

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        norm = []
        for raw in lines:
            s = _COMMENT.sub("", raw)
            s = _STRING.sub("", s)
            s = re.sub(r"\s+", "", s)
            if s and not s.startswith(("from ", "import ")):
                norm.append(s)
        total += len(norm)
        for i in range(len(norm) - win + 1):
            g = tuple(norm[i:i + win])
            if len(set(g)) < 3:  # 样板（如一行括号）不算重复
                continue
            seen[hash(g)].append((rel, i + 1))

    dup_lines = 0
    cross_file = 0
    for locs in seen.values():
        if len(locs) > 1:
            dup_lines += win * len(locs)
            if len({f for f, _ in locs}) > 1:
                cross_file += 1
    return {
        "lines-scanned": total,
        "dup-lines": dup_lines,
        "ratio": (dup_lines / total) if total else 0.0,
        "dup-blocks": sum(1 for v in seen.values() if len(v) > 1),
        "cross-file-blocks": cross_file,
    }


def report() -> dict:
    funcs, imports, file_lines = collect_python()
    dup = collect_duplication()

    # 指标名 -> 函数记录里的字段名（func-lines 的字段叫 "lines"）
    FIELD = {
        "cc-cognitive": "cc-cognitive",
        "cc-mccabe": "cc-mccabe",
        "func-lines": "lines",
        "nesting": "nesting",
        "max-args": "max-args",
    }

    def over(key: str) -> int:
        return sum(1 for f in funcs if f[FIELD[key]] > THRESHOLDS[key])

    metrics = {
        "functions": len(funcs),
        "cc-cognitive": {
            "threshold": THRESHOLDS["cc-cognitive"], "over": over("cc-cognitive"),
            "worst": sorted(funcs, key=lambda f: -f["cc-cognitive"])[:5],
        },
        "cc-mccabe": {
            "threshold": THRESHOLDS["cc-mccabe"], "over": over("cc-mccabe"),
            "worst": sorted(funcs, key=lambda f: -f["cc-mccabe"])[:5],
        },
        "func-lines": {
            "threshold": THRESHOLDS["func-lines"], "over": over("func-lines"),
            "worst": sorted(funcs, key=lambda f: -f["lines"])[:5],
        },
        "file-lines": {
            "threshold": THRESHOLDS["file-lines"],
            "over": sum(1 for v in file_lines.values() if v > THRESHOLDS["file-lines"]),
            "worst": sorted(file_lines.items(), key=lambda kv: -kv[1])[:5],
        },
        "nesting": {
            "threshold": THRESHOLDS["nesting"], "over": over("nesting"),
            "worst": sorted(funcs, key=lambda f: -f["nesting"])[:5],
        },
        # FastAPI 路由已豁免（is_fastapi_route），此处只报真超标
        "max-args": {
            "threshold": THRESHOLDS["max-args"], "over": over("max-args"),
            "worst": sorted((f for f in funcs if f["max-args"] > 0),
                            key=lambda f: -f["max-args"])[:5],
        },
        "duplication": {"threshold": THRESHOLDS["duplication"], **dup},
        "fan-out": {
            "threshold": THRESHOLDS["fan-out"],
            "over": sum(1 for v in imports.values() if len(v) > THRESHOLDS["fan-out"]),
            "worst": sorted(((k, len(v)) for k, v in imports.items()),
                            key=lambda kv: -kv[1])[:5],
        },
    }
    return metrics


def print_report(m: dict) -> None:
    W = 62
    print("=" * W)
    print("代码质量指标看板（8 维）—— docs/engineering-standards.md §4.2")
    print("看板存在 = 坏味道没被忽略。只报不拦是默认策略（存量超标是既成事实）。")
    print("=" * W)

    rows = [
        ("认知复杂度 CC", "cc-cognitive", "个函数超阈"),
        ("圈复杂度 McCabe", "cc-mccabe", "个函数超阈"),
        ("函数长度", "func-lines", "个函数超阈"),
        ("文件规模", "file-lines", "个文件超阈"),
        ("嵌套深度", "nesting", "个函数超阈"),
        ("长参数列表", "max-args", "个函数超阈（FastAPI 路由已豁免）"),
        ("耦合 fan-out", "fan-out", "个文件超阈"),
    ]
    print(f"\n统计范围：{m['functions']} 个函数（backend/app + backend/tools，已排除测试）\n")
    for label, key, unit in rows:
        d = m[key]
        flag = "OK " if d["over"] == 0 else "超阈"
        print(f"  [{flag}] {label:<16} 阈值 {d['threshold']:>4}   超阈 {d['over']:>4} {unit}")

    d = m["duplication"]
    flag = "OK " if d["ratio"] <= d["threshold"] else "超阈"
    print(f"  [{flag}] {'重复率':<16} 阈值 {d['threshold']:>4.0%}   实测 {d['ratio']:>4.1%}"
          f"（{d['dup-blocks']} 块，其中 {d['cross-file-blocks']} 跨文件）")

    print("\n最重的 5 个函数（认知复杂度 / 圈复杂度**必须成对读**）：")
    print(f"  {'认知':>5} {'圈':>5} {'嵌套':>5}  位置")
    by_cog = {f["file"] + ":" + str(f["line"]): f for f in m["cc-cognitive"]["worst"]}
    mcc = {f["file"] + ":" + str(f["line"]): f["cc-mccabe"] for f in m["cc-mccabe"]["worst"]}
    nest = {f["file"] + ":" + str(f["line"]): f["nesting"] for f in m["nesting"]["worst"]}
    for loc, f in by_cog.items():
        print(f"  {f['cc-cognitive']:>5} {mcc.get(loc, 0):>5} {nest.get(loc, 0):>5}  {loc} {f['name']}()")
    print("  判读：认知 ≫ 圈 → 深嵌套型，拆嵌套即可；两值都高 → 职责混杂，必须拆职责。")

    print("\n超过阈值的文件：")
    for f, n in m["file-lines"]["worst"]:
        mark = "超阈" if n > THRESHOLDS["file-lines"] else "  —"
        print(f"  [{mark}] {n:>5}  {f}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="8 维代码质量指标看板")
    ap.add_argument("--json", action="store_true", help="输出 JSON（CI artifact 用）")
    ap.add_argument("--fail-on", nargs="*", metavar="DIM=N",
                    help="仅当指定维度超过阈值时退出码为 1，如 cc-cognitive=300")
    args = ap.parse_args()

    metrics = report()
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print_report(metrics)

    for spec in args.fail_on or []:
        key, _, val = spec.partition("=")
        d = metrics.get(key)
        if d is None:
            continue
        actual = d.get("over") if isinstance(d.get("over"), int) else d.get("ratio")
        if actual is not None and actual > float(val):
            print(f"FAIL  {key} = {actual} 超过设定上限 {val}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
