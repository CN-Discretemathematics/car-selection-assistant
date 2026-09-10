#!/usr/bin/env python3
"""carSelection 仓库密钥扫描器（reviewer 配套工具）。

扫描仓库内文本文件，查找疑似 API Key、口令、Token、私钥、带凭据的数据库 URL
等机密字面量。供 Reviewer Agent 在每次审查前运行。

用法（在仓库根目录）:
    python reviewer/scan_secrets.py             # 全量扫描
    python reviewer/scan_secrets.py --verbose   # 显示每条命中的 file:line
    python reviewer/scan_secrets.py --json      # 输出 JSON 报告
    python reviewer/scan_secrets.py --path backend  # 只扫指定路径
    python reviewer/scan_secrets.py --strict-ignored  # gitignored 命中也计入退出码

退出码: 0 = 未发现「会入库的」可疑项; 1 = 发现可疑项（或参数错误）。

gitignore 语义（评审 m13）：REVIEWER_AGENT.md §1 认可的正确做法是「密钥只存在于
本地未跟踪的 .env（已被 .gitignore 忽略）」，因此位于 gitignored 路径的命中以
INFO 呈现、不计入退出码——否则任何开发机上都恒为退出码 1，上线检查清单
（deploy/ALIYUN_RUNBOOK.md §7）永远无法达成。已被 git 跟踪的文件不受此豁免。
需要严格审计时用 --strict-ignored。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ── 默认忽略的目录 / 文件（与 .gitignore 保持一致，另加依赖与构建产物） ──────────
DEFAULT_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".next",
    "out",
    ".venv",
    "venv",
    ".deps",          # 本仓库本地 Python 依赖（backend/.deps）
    ".wheels",        # 依赖 wheel 缓存（backend/.wheels）
    ".npm-cache",     # npm 缓存（cacache 内容寻址文件，含高熵哈希）
    ".pnpm-store",    # pnpm 内容寻址缓存（哈希文件名，非交付物）
    ".tmp",           # 临时探测脚本/抓包缓存（gitignored，不作为交付物扫描）
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
}
DEFAULT_IGNORE_FILES = {
    "*.pyc",
    "*.pyo",
    "*.so",
    "*.dll",
    "*.exe",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.ico",
    "*.pdf",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.lock",         # 依赖锁文件可包含哈希，但不含密钥
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

# 只允许存在的环境变量模板文件（占位符），真实 .env* 一律告警
ENV_TEMPLATE_ALLOWED = {".env.example", ".env.sample", ".env.template", ".env.example.local"}

# 扫描器自身路径：不扫描自己（否则其规则定义/说明文本会自命中）
SCANNER_SELF = Path(__file__).resolve()

# 占位符值：值形如这些时不算密钥
PLACEHOLDER_PATTERNS = re.compile(
    r"^(your[-_]?|example[-_]?|xxx+|<[^>]+>|changeme|placeholder|dummy|test[-_]?key|"
    r"sk[-_]?example|sk[-_]?your|put[-_]?your|replace[-_]?me|TODO|\.\.\.)",
    re.IGNORECASE,
)

# ── 检测规则：name, severity, regex, 说明 ──────────────────────────────────────
RULES = [
    (
        "deepseek-openai-api-key",
        "HIGH",
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        "疑似 DeepSeek/OpenAI 风格 API Key 字面量（sk-...）",
    ),
    (
        "aws-access-key",
        "HIGH",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "疑似 AWS Access Key ID（AKIA...）",
    ),
    (
        "github-token",
        "HIGH",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "疑似 GitHub Personal Access Token（ghp_/gho_/ghu_/ghs_/ghr_...）",
    ),
    (
        "google-api-key",
        "HIGH",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "疑似 Google API Key（AIza...）",
    ),
    (
        "slack-token",
        "HIGH",
        re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
        "疑似 Slack Token（xoxb-/xoxa-/xoxp-/xoxr-/xoxs-...）",
    ),
    (
        "private-key-block",
        "HIGH",
        re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
        "疑似私钥块",
    ),
    (
        "db-url-with-credentials",
        "HIGH",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mariadb|redis|mongodb(?:\+srv)?|amqp)\+?\w*://"
            r"[^/\s:@]+:[^@\s/]+@",
            re.IGNORECASE,
        ),
        "带用户名:口令的数据库/消息队列 URL",
    ),
    (
        "jwt-token",
        "MEDIUM",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "疑似 JWT Token（eyJ...）",
    ),
    (
        "generic-secret-assignment",
        "MEDIUM",
        re.compile(
            r"\b(?:api[_-]?key|apikey|access[_-]?key|secret|secret[_-]?key|client[_-]?secret|"
            r"password|passwd|pwd|token|auth[_-]?token|bearer)\b\s*[:=]\s*"
            r"['\"]([^'\"]{8,})['\"]",
            re.IGNORECASE,
        ),
        "疑似给密钥类变量直接赋值字符串字面量",
    ),
    (
        "nv-embedded-credentials",
        "MEDIUM",
        re.compile(r"(?:DEEPSEEK|OPENAI|ANTHROPIC|GEMINI|MISTRAL|AWS|AZURE)_?[A-Z_]*(?:KEY|SECRET|TOKEN)\s*=\s*['\"][^'\"]{8,}['\"]"),
        "疑似在文件里硬编码 LLM/云厂商密钥环境变量",
    ),
]

# 高熵 token 补充检测：16+ 位 base64/hex 出现在密钥类变量名旁边
HIGH_ENTROPY = re.compile(r"\b[a-zA-Z0-9+/_-]{24,}={0,2}\b")


def _is_ignored(path: Path, ignore_dirs: set[str]) -> bool:
    parts = path.parts
    for i, part in enumerate(parts):
        if part in ignore_dirs:
            return True
        # 仅忽略 backend/vendor（第三方依赖目录）；其他位置的 vendor/ 照常扫描
        if part == "vendor" and i > 0 and parts[i - 1] == "backend":
            return True
    return False


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(8192)
        return b"\x00" in head
    except OSError:
        return True


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_PATTERNS.match(value.strip())) or len(value.strip()) < 8


def _is_placeholder_url(url: str) -> bool:
    """带凭据 URL 是否只是文档示例（含 ...、xxx、example、<...> 等占位标记）。"""
    return bool(
        re.search(r"(\.\.\.|xxx+|example|your[-_]|<[^>]+>|@\.\.\.)", url, re.IGNORECASE)
    )


def _git_ignored_paths(root: Path, rel_paths: list[str]) -> set[str]:
    """返回其中被 .gitignore 忽略的路径（posix 形式）。

    非 git 仓库、git 不可用或超时 → 返回空集（即全部命中都计入，保守）。
    已被 git 跟踪的文件不会被 check-ignore 报告为忽略，因此不享受豁免。
    路径走命令行参数而非 stdin 管道（Windows 沙箱下子进程 stdin 管道可能不可用，
    评审 m13 实测），并分批传入避免命令行长度限制。
    """
    candidates = sorted({p.replace(os.sep, "/") for p in rel_paths if p})
    ignored: set[str] = set()
    for start in range(0, len(candidates), 100):
        batch = candidates[start:start + 100]
        try:
            proc = subprocess.run(
                ["git", "check-ignore", *batch],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        # returncode 0 = 至少一个路径被忽略（列在 stdout）；1 = 全部未被忽略
        if proc.returncode == 0 and proc.stdout:
            ignored.update(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return ignored


def scan_path(root: Path, verbose: bool = False) -> list[dict]:
    findings: list[dict] = []
    # os.walk 剪枝：跳过 node_modules/.git/.tmp 等大目录，避免无谓遍历拖慢扫描
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_ignored(Path(dirpath) / d, DEFAULT_IGNORE_DIRS)]
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.resolve() == SCANNER_SELF:
                continue
            if _is_ignored(path, DEFAULT_IGNORE_DIRS):
                continue
            if any(path.match(pat) for pat in DEFAULT_IGNORE_FILES):
                continue
            if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                continue

            # 环境变量文件检查（非模板）
            if path.name.startswith(".env") and path.name not in ENV_TEMPLATE_ALLOWED:
                findings.append(
                    {
                        "file": str(path.relative_to(root)),
                        "line": 0,
                        "rule": "env-file",
                        "severity": "HIGH",
                        "detail": "检测到环境变量文件（.env*），不应被上传/提交；仅允许 .env.example 等占位模板",
                    }
                )
                continue

            if _is_binary(path):
                continue

            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for lineno, line in enumerate(lines, start=1):
                for rule_name, severity, pattern, detail in RULES:
                    for m in pattern.finditer(line):
                        # 密钥赋值类规则：跳过占位符值
                        if rule_name == "generic-secret-assignment" and m.lastindex and _is_placeholder(m.group(1)):
                            continue
                        # sk- 类：值本身是占位符则跳过
                        if rule_name == "deepseek-openai-api-key" and _is_placeholder(m.group(0)):
                            continue
                        # 带凭据 URL：文档示例（...、xxx、example、<...>）不告警；
                        # 正则只匹配到 @，占位标记常在 URL 之后，因此按整行判断
                        if rule_name == "db-url-with-credentials" and _is_placeholder_url(line):
                            continue
                        findings.append(
                            {
                                "file": str(path.relative_to(root)),
                                "line": lineno,
                                "rule": rule_name,
                                "severity": severity,
                                "detail": detail,
                            }
                        )
                # 高熵 token 跟随密钥类变量名（如 DEEPSEEK_API_KEY="..." 已被 nv 规则覆盖）
                # 这里补充：行内同时出现密钥类变量名与高熵字符串且不在占位符集合
                if HIGH_ENTROPY.search(line):
                    lowered = line.lower()
                    if re.search(r"(api[_-]?key|secret|token|password|passwd)", lowered):
                        for m in HIGH_ENTROPY.finditer(line):
                            if not _is_placeholder(m.group(0)):
                                findings.append(
                                    {
                                        "file": str(path.relative_to(root)),
                                        "line": lineno,
                                        "rule": "high-entropy-secret",
                                        "severity": "MEDIUM",
                                        "detail": "密钥类变量附近出现疑似高熵字符串，需人工确认",
                                    }
                                )
                                break
    return findings


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，强制 UTF-8 输出避免 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="扫描仓库中的密钥与机密字面量")
    parser.add_argument("--path", default=".", help="要扫描的根路径（默认当前目录）")
    parser.add_argument("--verbose", action="store_true", help="逐条打印命中项")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--quiet", action="store_true", help="只输出结论，不输出命中明细")
    parser.add_argument(
        "--strict-ignored", action="store_true",
        help="gitignored 路径的命中也计入退出码（严格审计用）",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"错误：路径不存在或不是目录: {root}", file=sys.stderr)
        return 1

    findings = scan_path(root, verbose=args.verbose)
    ignored = set() if args.strict_ignored else _git_ignored_paths(root, [f["file"] for f in findings])
    for f in findings:
        f["gitignored"] = f["file"].replace(os.sep, "/") in ignored
    blocking = [f for f in findings if not f["gitignored"]]
    local_only = [f for f in findings if f["gitignored"]]

    if args.json:
        print(json.dumps(
            {
                "findings": findings,
                "count": len(findings),
                "blocking_count": len(blocking),
                "gitignored_count": len(local_only),
            },
            ensure_ascii=False, indent=2,
        ))
    elif args.verbose or not args.quiet:
        for f in blocking:
            loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            print(f"[{f['severity']}] {loc}  [{f['rule']}] {f['detail']}")
        for f in local_only:
            loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            print(f"[INFO] {loc}  [{f['rule']}] 已被 .gitignore 忽略、不会入库；{f['detail']}")
        summary = (
            f"\n共发现 {len(blocking)} 个会入库的可疑项" if blocking
            else "\n未发现会入库的可疑项 ✓"
        )
        if local_only:
            summary += f"；另有 {len(local_only)} 项位于 gitignored 本地文件（仅供参考，--strict-ignored 可严格审计）"
        print(summary)

    if blocking:
        print("\n结论：发现会入库的可疑密钥/机密字面量，禁止上传或提交！请修复后重新扫描。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
