# -*- coding: utf-8 -*-
"""doc-sync：公开文档与代码库一致性检查（skills/doc-sync.md 的可执行部分）。

用法（任意工作目录）：
    python skills/doc_sync_check.py            # 输出报告；存在 FAIL 时退出码 1

只读检测；自动对齐由 Agent 按 skill 文档执行。本脚本给出**可判定的证据**：
  1. 全量用例数：pytest 收集数 vs 文档「pytest ... N 用例」声明（CHANGES.md 为历史快照，豁免）
  2. 迁移数量：alembic/versions/*.py 实数 vs 文档「N 个迁移」
  3. LLM 工具 schema 数：TOOL_SCHEMAS 实数 vs 文档「N 个 LLM 工具 schema」
  4. README 项目结构：backend/app 子模块清单 vs 实际目录
  5. 文档反引号路径引用：是否存在悬空（如已删除却仍被引用的脚本）
  6. 环境变量：文档表格里的 VAR 是否在代码/示例配置中真实可读
  7. 编码卫生：全部项目 .md 必须是合法 UTF-8 无 BOM（拦截 PowerShell 重定向写出的 UTF-16）
  8. 已知过期表述（stale patterns）：出现即 FAIL
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "vendor", ".tools", ".next", ".pnpm-store",
    ".dsh-test", ".arkcli-install", ".deploy", "__pycache__", ".tmp", ".zcode",
    ".pytest_cache", ".wheels", "snapshots", "logs",
}
DOC_DIRS = [ROOT] + [ROOT / d for d in ("docs", "skills", "deploy", "reviewer", "resume")]
# 历史快照/事故记录文件：旧数字与过期字符串是**有意引用**的案例，豁免计数与过期表述检查
HISTORICAL_FILES = {ROOT / "CHANGES.md", ROOT / "skills" / "doc-sync.md"}
# 内部评测产物/依赖：不入仓库，全部豁免
EXEMPT_PREFIXES = (ROOT / "backend" / "eval", ROOT / "backend" / ".venv")

STALE_PATTERNS = [
    "路权重按查询类型",       # 已改为统一 0.6/0.4（RAG.md §2）
    "路权重按 query_type",    # 同上（RAG_TECH_SELECTION 旧文）
    "deploy/systemd",         # 非容器方案已废弃，deploy/ 下无该目录
    "deploy.sh 自动安装",     # systemd 装载路径已不存在
]

# 行内出现这些词 = 有意的历史/删除记录，跳过该行的悬空引用与过期表述检查
ALLOW_WORDS = ("删除", "移除", "取代", "一次性", "已完成使命", "历史", "曾", "此前",
               "旧", "改用", "已废弃", "清零")
# 反引号 token 命中这些子串 = 占位/模板/通配，跳过
TOKEN_SKIP_SUBSTR = ("...", "<", "{", "}", "*", "label:", "your-", "xxx", "XXX", " ")
# 运行期/本地产物前缀：文档提及但仓库不保证存在，跳过悬空检查
RUNTIME_PREFIXES = (
    "backend/logs/", "backend/snapshots/", "backend/.tmp/", "backend/.env",
    "backend/vendor/", "backend/.venv/", "backend/.embed_cache", "backend/eval/",
)

fails: list[str] = []
warns: list[str] = []


def fail(msg: str) -> None:
    fails.append(msg)


def warn(msg: str) -> None:
    warns.append(msg)


def iter_project_md() -> list[Path]:
    out: list[Path] = []
    for base in DOC_DIRS:
        if not base.exists():
            continue
        for p in base.rglob("*.md"):
            rel = p.relative_to(ROOT)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if any(rel.is_relative_to(pre) for pre in EXEMPT_PREFIXES):
                continue
            out.append(p)
    return sorted(set(out))


def collect_docs() -> list[tuple[Path, list[str]]]:
    docs = []
    for p in iter_project_md():
        try:
            docs.append((p, p.read_text(encoding="utf-8").splitlines()))
        except UnicodeDecodeError:
            continue  # 编码检查会另行报告
    return docs


# ── 1. 用例数 ────────────────────────────────────────────────────────────────
def actual_test_count() -> int | None:
    py = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = ROOT / "backend" / ".venv" / "bin" / "python"
    if not py.exists():
        warn("未找到 backend/.venv，跳过用例数核对")
        return None
    try:
        proc = subprocess.run(
            [str(py), "-m", "pytest", "--collect-only", "-q"],
            cwd=ROOT / "backend", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        warn("pytest 收集失败/超时，跳过用例数核对")
        return None
    m = re.search(r"(\d+) tests? collected", proc.stdout or "")
    return int(m.group(1)) if m else None


def check_test_count(actual: int | None, docs: list[tuple[Path, list[str]]]) -> None:
    if actual is None:
        return
    pat = re.compile(r"pytest[^\n，。；]{0,60}?(\d{2,4})\s*用例")
    pat_gate = re.compile(r"(\d{2,4})\s*用例与两道静态门禁")
    for path, lines in docs:
        if path in HISTORICAL_FILES:
            continue
        for i, line in enumerate(lines, 1):
            for m in list(pat.finditer(line)) + list(pat_gate.finditer(line)):
                n = int(m.group(1))
                if n != actual:
                    fail(f"用例数不符 {path.relative_to(ROOT)}:{i} 文档写 {n}，实际 {actual}")


# ── 2. 迁移数 ────────────────────────────────────────────────────────────────
def check_migration_count(docs: list[tuple[Path, list[str]]]) -> None:
    versions = ROOT / "backend" / "alembic" / "versions"
    actual = len([p for p in versions.glob("*.py") if p.name != "__init__.py"]) if versions.exists() else 0
    for path, lines in docs:
        if path in HISTORICAL_FILES:
            continue
        for i, line in enumerate(lines, 1):
            for m in re.finditer(r"(\d+)\s*个迁移", line):
                n = int(m.group(1))
                if n != actual:
                    fail(f"迁移数不符 {path.relative_to(ROOT)}:{i} 文档写 {n}，实际 {actual}")


# ── 3. 工具 schema 数 ────────────────────────────────────────────────────────
def check_tool_count(docs: list[tuple[Path, list[str]]]) -> None:
    tools_py = ROOT / "backend" / "app" / "agent" / "tools.py"
    text = tools_py.read_text(encoding="utf-8") if tools_py.exists() else ""
    actual = len(set(re.findall(r'"name":\s*"(\w+)"', text)))
    for path, lines in docs:
        for i, line in enumerate(lines, 1):
            for m in re.finditer(r"(\d+)\s*个?\s*LLM 工具 schema", line):
                n = int(m.group(1))
                if n != actual:
                    fail(f"工具 schema 数不符 {path.relative_to(ROOT)}:{i} 文档写 {n}，实际 {actual}")


# ── 4. README 项目结构 vs 实际目录 ───────────────────────────────────────────
def check_readme_structure() -> None:
    readme = ROOT / "README.md"
    if not readme.exists():
        fail("README.md 不存在")
        return
    try:
        lines = readme.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return  # 编码问题由 check_encoding() 报 FAIL，这里跳过结构核对而不是抛栈
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## 项目结构")
    except StopIteration:
        fail("README.md 缺少「## 项目结构」小节")
        return
    block: list[str] = []
    in_block = False
    for line in lines[start + 1:]:
        if line.strip().startswith("```"):
            if in_block:
                break
            in_block = True
            continue
        if in_block:
            block.append(line)

    documented: set[str] = set()
    seen_backend = False
    for line in block:
        if line.startswith("backend/"):
            seen_backend = True
            continue
        if seen_backend:
            if line.startswith("web/") or not line.strip():
                break
            m = re.match(r"^    ([\w.]+)/", line)
            if m:
                documented.add(m.group(1))

    actual = {p.name for p in (ROOT / "backend" / "app").iterdir()
              if p.is_dir() and p.name != "__pycache__"}
    missing = sorted(actual - documented)
    extra = sorted(documented - actual)
    if missing:
        fail(f"README 项目结构缺少 backend/app 模块: {missing}")
    if extra:
        fail(f"README 项目结构列出但不存在于 backend/app 的模块: {extra}")


# ── 5. 反引号路径悬空检查 ─────────────────────────────────────────────────────
PATH_TOKEN = re.compile(r"`([^`\n]+)`")
ROOT_PREFIXES = ("backend/", "web/", "deploy/", "docs/", "skills/", "reviewer/", "resume/")
BACKEND_REL_PREFIXES = ("app/", "tools/", "tests/", "alembic/")
ROOT_MD_FILES = {
    "README.md", "RAG.md", "CHANGES.md", "DELIVERY.md", "DEPLOYMENT.md",
    "OPS_GUIDE.md", "PROJECT_PLAN.md", "RAG_TECH_SELECTION.md", "LICENSE",
}


def token_exists(token: str) -> bool:
    candidates: list[Path] = []
    if token.startswith(ROOT_PREFIXES):
        candidates.append(ROOT / token)
    if token.startswith(BACKEND_REL_PREFIXES):
        candidates.append(ROOT / "backend" / token)
    if "/" not in token and token in ROOT_MD_FILES:
        candidates.append(ROOT / token)
    return any(c.exists() for c in candidates)


def check_dangling_refs(docs: list[tuple[Path, list[str]]]) -> None:
    for path, lines in docs:
        if path in HISTORICAL_FILES:
            continue
        for i, line in enumerate(lines, 1):
            if any(w in line for w in ALLOW_WORDS):
                continue
            for m in PATH_TOKEN.finditer(line):
                token = m.group(1).strip().rstrip("。：,，;；)）").rstrip("/")
                token = token.split("::", 1)[0]  # 「path.py::func」只核对文件部分
                if not token or any(s in token for s in TOKEN_SKIP_SUBSTR):
                    continue
                # 白名单写成带尾斜杠的前缀，但 token 已 rstrip("/")，故需同时比对去斜杠形式
                if token.startswith("/") or any(
                    token == p.rstrip("/") or token.startswith(p) for p in RUNTIME_PREFIXES
                ):
                    continue
                relevant = (token.startswith(ROOT_PREFIXES)
                            or token.startswith(BACKEND_REL_PREFIXES)
                            or ("/" not in token and token in ROOT_MD_FILES))
                if not relevant:
                    continue
                if not token_exists(token):
                    fail(f"悬空路径引用 {path.relative_to(ROOT)}:{i} `{token}` 不存在")


# ── 6. 环境变量存在性 ────────────────────────────────────────────────────────
ENV_ALLOW = {"LANGSMITH_API_KEY", "LANGSMITH_TRACING"}  # LangChain 运行时直接读取
ENV_TOKEN = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`")


def env_sources_text() -> str:
    chunks: list[str] = []
    for rel in (
        "backend/.env.example", "backend/app/common/config.py",
        "backend/app/retrieval/config.py", "web/.env.example", "web/next.config.mjs",
    ):
        p = ROOT / rel
        if p.exists():
            chunks.append(p.read_text(encoding="utf-8", errors="replace"))
    for base in (ROOT / "backend" / "app", ROOT / "backend" / "tools",
                 ROOT / "web" / "app", ROOT / "web" / "lib"):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.suffix in {".py", ".ts", ".tsx", ".mjs"} and p.is_file():
                try:
                    chunks.append(p.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
    return "\n".join(chunks)


# 环境变量检查只针对「会写配置表」的文档，避免把内部评审文档的枚举值误判为环境变量
ENV_DOC_NAMES = {"README.md", "RAG.md", "RAG_TECH_SELECTION.md", "DELIVERY.md",
                 "DEPLOYMENT.md", "OPS_GUIDE.md", "deployment.md", "aliyun-ops.md",
                 "credential-rotation.md"}


def check_env_vars(docs: list[tuple[Path, list[str]]], source: str, source_lower: str) -> None:
    for path, lines in docs:
        if path.name not in ENV_DOC_NAMES:
            continue
        for i, line in enumerate(lines, 1):
            if not line.lstrip().startswith(("|", "-")):  # 只查表格/列表里的变量引用
                continue
            for m in ENV_TOKEN.finditer(line):
                var = m.group(1)
                if var in ENV_ALLOW:
                    continue
                # pydantic-settings 字段为小写（deepseek_base_url），大小写不敏感匹配
                if var.lower() not in source_lower:
                    fail(f"环境变量存疑 {path.relative_to(ROOT)}:{i} `{var}` 在代码/示例配置中未找到")


# ── 7. 编码卫生 ──────────────────────────────────────────────────────────────
def check_encoding() -> None:
    for p in iter_project_md():
        raw = p.read_bytes()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            fail(f"编码错误 {p.relative_to(ROOT)} 是 UTF-16（PowerShell 重定向产物），须转存 UTF-8")
            continue
        if raw.startswith(b"\xef\xbb\xbf"):
            warn(f"编码提示 {p.relative_to(ROOT)} 带 UTF-8 BOM（仓库惯例为无 BOM）")
            raw = raw[3:]
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as e:
            fail(f"编码错误 {p.relative_to(ROOT)} 不是合法 UTF-8（{e}）")


# ── 8. 已知过期表述 ──────────────────────────────────────────────────────────
def check_stale_patterns(docs: list[tuple[Path, list[str]]]) -> None:
    for path, lines in docs:
        if path in HISTORICAL_FILES:
            continue
        for i, line in enumerate(lines, 1):
            if any(w in line for w in ALLOW_WORDS):
                continue
            for pat in STALE_PATTERNS:
                if pat in line:
                    fail(f"过期表述 {path.relative_to(ROOT)}:{i} 含「{pat}」")


def main() -> int:
    # 编码检查必须最先跑：文件是 UTF-16 时，后续任何 read_text 都会抛 UnicodeDecodeError，
    # 把这条唯一能解释事故的诊断淹成 traceback。
    check_encoding()
    docs = collect_docs()
    count = actual_test_count()
    check_test_count(count, docs)
    check_migration_count(docs)
    check_tool_count(docs)
    check_readme_structure()
    check_dangling_refs(docs)
    env_source = env_sources_text()
    check_env_vars(docs, env_source, env_source.lower())
    check_stale_patterns(docs)

    print(f"doc-sync 检查报告（pytest 实测：{count if count is not None else '跳过'}）")
    for w in warns:
        print(f"WARN  {w}")
    for f_ in fails:
        print(f"FAIL  {f_}")
    print(f"结论：{'一致' if not fails else f'{len(fails)} 处不一致'}（{len(docs)} 份文档纳入检查）")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
