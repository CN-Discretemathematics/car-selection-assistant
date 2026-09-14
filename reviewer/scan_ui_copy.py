"""用户界面/接口文案门禁：公开页面与后端返回文案不得出现内部术语与实现说明。

为什么需要（2026-09-14 事故，两次）：详情页脚注曾写着「缺失数据统一显示「官方资料未披露」，
不做猜测补全。」——这是写给我们自己看的实现说明；第一次清理只查了固定术语表（SKU/§…），
这句话没有术语词，整句漏过，直到用户再次反馈才发现。第二次是后端：对比页直接展示的
API 错误信息里仍有「SKU 不存在」（`comparison/router.py`）。

检查范围与口径：
- 前端：`web/app`、`web/lib`（排除 `/ops` 运维页与仅它使用的 `web/lib/rag.ts`）；
  `.tsx/.ts` 一律扫描，整行注释跳过。
- 后端：`backend/app/**/*.py` 只扫**用户可见字符串**——注释与 docstring 里的 SKU 等
  技术术语是给维护者的，允许保留；故按三引号状态机排除 docstring，再跳过整行注释。

用法（仓库根目录）：

    python reviewer/scan_ui_copy.py            # 干净则退出码 0
    python reviewer/scan_ui_copy.py --verbose  # 逐条打印命中位置

与 `reviewer/scan_secrets.py` 一样属于「提交前必过」的静态门禁。
"""
from __future__ import annotations

import ast
import pathlib
import sys

# ── 前端 ────────────────────────────────────────────────────────────────────
UI_ROOTS = ("web/app", "web/lib")
# 内部页面专用模块：web/lib/rag.ts 只被 /ops/rag 引用，其中「SKU 事实」等标签属运维页
INTERNAL_FILES = ("web/lib/rag.ts",)

# ── 后端 ────────────────────────────────────────────────────────────────────
BACKEND_ROOT = "backend/app"
BACKEND_JARGON = ("SKU", "§", "门户口径", "幂等", "落库")
BACKEND_PHRASES = ("不做猜测补全", "猜测补全", "统一显示", "内部实现", "口径统一")

# ── 通用规则 ────────────────────────────────────────────────────────────────
JARGON = ("SKU", "§", "门户口径", "范围内", "哈希", "幂等", "入库", "抓取")
IMPLEMENTATION_PHRASES = (
    "不做猜测补全", "猜测补全", "统一显示", "内部实现", "落库", "口径统一",
    "抓取脚本", "抓取失败", "字段映射",
)
SKIP_PREFIX = ("//", "{/*", "*", "/**", "/*")


def _frontend_hits(root: pathlib.Path) -> list[str]:
    hits: list[str] = []
    files: set[pathlib.Path] = set()
    for base in UI_ROOTS:
        directory = root / base
        if directory.is_dir():
            files.update(directory.rglob("*.tsx"))
            files.update(directory.rglob("*.ts"))
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        if "/ops/" in rel or rel.startswith("ops/") or rel in INTERNAL_FILES:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(SKIP_PREFIX):
                continue
            matched = next((t for t in JARGON if t in line), None)
            if matched is None:
                matched = next((p for p in IMPLEMENTATION_PHRASES if p in line), None)
            if matched is not None:
                hits.append(f"{rel}:{lineno}: [{matched}] {stripped[:110]}")
    return hits


def _backend_hits(root: pathlib.Path) -> list[str]:
    """只扫**引号内的中文串**（用户可见文案）。

    这样能区分三类：
    - 用户可见：`raise bad_request("SKU 不存在")`、`label = "门户口径"` → 命中；
    - 技术标识符：`SKU_API = "https://…"`、`SKU_PAGE_TEMPLATE` → 术语在引号外 → 不命中；
    - 注释与 docstring：整行跳过（技术术语是给维护者的）。
    """
    hits: list[str] = []
    base = root / BACKEND_ROOT
    if not base.is_dir():
        return hits
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as err:
            hits.append(f"{rel}: [SyntaxError] 无法解析：{err.msg}")
            continue
        # docstring 语义排除（module/class/function 的首个字符串表达式）
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    if isinstance(body[0].value.value, str):
                        docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            text = node.value
            matched = next((t for t in BACKEND_JARGON if t in text), None)
            if matched is None:
                matched = next((p for p in BACKEND_PHRASES if p in text), None)
            if matched is not None:
                hits.append(f"{rel}:{node.lineno}: [{matched}] {text.strip()[:110]}")
    return hits


def scan(root: pathlib.Path | None = None) -> list[str]:
    root = root or pathlib.Path(__file__).resolve().parents[1]
    return _frontend_hits(root) + _backend_hits(root)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    verbose = "--verbose" in args
    hits = scan()
    if not hits:
        # 只用 ASCII 标记：Windows 控制台默认 GBK，打印 ✓ 会 UnicodeEncodeError（实测）
        print("[OK] 前端与后端用户可见文案无内部术语与实现说明")
        return 0
    for hit in hits if verbose else hits[:20]:
        print(f"  {hit}", file=sys.stderr)
    if not verbose and len(hits) > 20:
        print(f"  …（共 {len(hits)} 处，--verbose 查看全部）", file=sys.stderr)
    print(f"[FAIL] 发现 {len(hits)} 处内部表述出现在用户可见文案", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
