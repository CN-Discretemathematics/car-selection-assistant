# -*- coding: utf-8 -*-
"""pre-push 提交批门禁 —— 防「并行会话提交串台」事故复发。

背景（2026-09-16 实测事故）：两个会话并行开发不同功能，共用 .tools/commit-msg.txt 写
提交信息，后提交方把先写进文件的信息原样带走（compare 功能提交顶着前端美化轮的信息
被推上远端）。本脚本在 push 前对「将要推上去的每个提交」做机械化核对：

  R1 提交标题必须是 `type(scope): 摘要` 形式（merge / revert 提交跳过）；
  R2 改动文件必须落在其 scope 声明的路径前缀（或中性路径）之内 —— 本条即可抓住
     「信息顶包」：compare 的代码 + feat(web) 的信息必然出现越界文件；
  R3 每个 scope token 至少要有一个实际改动文件与之匹配（防 scope 写错/写空）；
  R4 同一批 push 内不允许出现两条完全相同的标题（顶包的典型伴随症状）。

用法：
  # 钩子模式（由 .githooks/pre-push 调用，stdin 传 ref/sha）
  python tools/pre-push-guard.py

  # 手动模式：默认核对 当前分支 upstream..HEAD
  python tools/pre-push-guard.py
  # 手动模式：显式指定 rev-list 范围或单个 sha
  python tools/pre-push-guard.py origin/main..HEAD
  python tools/pre-push-guard.py de7985a            # 事故回归探针：预期 FAIL

豁免：确属跨切面的合法提交，用 PUSH_GUARD_ALLOW=1 或 --allow（输出会注明人工放行）；
或本次 push 用 `git push --no-verify` 跳过。豁免是显式动作，不留默认后门。

退出码：0=通过；1=拦截；2=环境/用法错误。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# ---------------------------------------------------------------- scope 映射
# scope token → 允许的改动路径前缀。依据仓库实际结构维护；新增 scope 时在此登记。
SCOPE_PATHS: dict[str, list[str]] = {
    "web": ["web/"],
    "ui": ["web/"],
    "compare": [
        "backend/app/comparison/",
        "web/app/compare/",
        "skills/sku-comparison.md",
        "backend/tests/test_comparison",
        "backend/tests/test_compare",
    ],
    # 事实数据正确性（同键冲突值 / 解析去重 / 存量修复 / 引擎存疑守卫）：
    # 跨 comparison + sources + tools，故单列 scope（2026-09-16 卡罗拉锐放假差异事故）
    "facts": [
        "backend/app/comparison/",
        "backend/app/sources/",
        "backend/tools/",
        "backend/tests/test_comparison",
        "backend/tests/test_autohome",
        "backend/tests/test_dedupe",
        "web/app/compare/",
        "skills/data-import-validation.md",
    ],
    "vehicles": [
        "backend/app/vehicles/",
        "backend/app/variants/",
        "backend/app/catalog/",
        "web/app/vehicles/",
        "backend/tests/test_vehicles",
        "backend/tests/test_variant",
        "backend/tests/test_autohome",
    ],
    "agent": [
        "backend/app/agent/",
        "backend/tests/test_agent",
        "backend/tests/test_tool_loop",
        "backend/tests/test_brand_constraint.py",
        "backend/tests/test_routing_golden.py",
        "backend/tests/routing_golden.jsonl",
        "web/app/components/AgentChat.tsx",
        # W0-P0 路由可观测化接线：启动日志装配与入口（2026-09-17，具体文件而非目录）
        "backend/app/main.py",
        "backend/app/common/logging_setup.py",
        "backend/tests/test_log_setup.py",
        # W0-P2 思考档位：LLMClient.chat 的 thinking 参数只由路由调用方使用
        "backend/app/common/llm.py",
        "skills/",
    ],
    "search": ["web/app/components/", "web/app/page.tsx", "backend/app/retrieval/"],
    "home": ["web/app/page.tsx", "web/app/components/", "web/app/layout.tsx"],
    "rag": [
        "backend/app/rag/",
        "backend/app/retrieval/",
        "backend/app/images/",
        "backend/eval/",
        "backend/tests/test_rag",
        "backend/tests/test_retrieval",
    ],
    "data": [
        "backend/app/catalog/",
        "backend/app/sales/",
        "backend/app/sources/",
        "backend/app/brands/",
        "backend/app/images/",
        "backend/tests/test_autohome",
        "backend/tests/test_augment",
        "backend/tests/test_sales",
    ],
    "security": [
        "backend/app/auth/",
        "backend/app/admin/",
        "backend/tests/test_auth",
        "backend/tests/test_admin",
        "reviewer/",
    ],
    "ops": ["web/app/ops/", "deploy/", ".github/", "backend/tests/test_ops"],
    "deploy": ["deploy/", ".github/", "docs/"],
    "reviewer": ["reviewer/", "backend/tests/test_reviewer", "skills/"],
    "skills": ["skills/"],
    "sync": ["docs/", "skills/", "README"],
    "docs": ["docs/", "skills/", "AGENTS.md", "README", "tools/", ".githooks/", "LICENSE"],
    "hygiene": ["tools/", ".githooks/", "skills/", "docs/", "AGENTS.md", "reviewer/"],
    "chore": [".github/", ".gitignore", "tools/", ".githooks/", "README", "LICENSE"],
    "test": ["backend/tests/", "web/"],
}

# 中性路径：任何 scope 都允许触碰（跨切面的测试 / 文档 / CI / 根级工具）。
NEUTRAL_PREFIXES = [
    "backend/tests/",
    "docs/",
    ".github/",
    "reviewer/",
    "README",
    "LICENSE",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
]

SUBJECT_RE = re.compile(r"^(feat|fix|refactor|docs|test|chore|style|perf|build|ci|ops|revert)\(([^)]+)\):\s*(\S.*)$")


def run_git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout


def resolve_batch(args: list[str]) -> list[str]:
    """把 CLI 参数 / hook stdin 解析成「待推送提交」的 sha 列表（旧→新）。"""
    shas: list[str] = []
    if args:
        for arg in args:
            if re.fullmatch(r"[0-9a-f]{7,40}", arg):
                shas += run_git("rev-list", "--reverse", "--no-walk", arg).split()
            else:
                shas += run_git("rev-list", "--reverse", arg).split()
        return shas
    # 无参数：hook 模式优先（git >=2.55 会另传（远端名, 远端地址）两个 CLI 参数，
    # 与 rev-spec 无关，一律忽略）；stdin 无内容时回退默认 upstream..HEAD。
    if not sys.stdin.isatty():
        lines = sys.stdin.read().splitlines()
        if lines:
            shas = _shas_from_push_lines(lines)
            if shas is not None:
                return shas
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if upstream.returncode == 0 and upstream.stdout.strip():
        shas = run_git("rev-list", "--reverse", f"{upstream.stdout.strip()}..HEAD").split()
    else:  # 无 upstream 的本地分支：退而核对相对 origin/main 的新增提交
        shas = run_git("rev-list", "--reverse", "origin/main..HEAD").split()
    return shas


def commit_info(sha: str) -> tuple[list[str], str, bool]:
    """返回 (改动文件列表, 标题, 是否 merge)。"""
    parents = run_git("rev-list", "--parents", "-n", "1", sha).split()[1:]
    subject = run_git("log", "-1", "--format=%s", sha).strip()
    if len(parents) > 1:
        return [], subject, True
    files = [
        f.replace("\\", "/")
        for f in run_git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha).splitlines()
        if f.strip()
    ]
    return files, subject, False


def _shas_from_push_lines(lines: list[str]) -> list[str] | None:
    """解析 git push 写入 stdin 的 ref 行 → 待推送 sha 列表（旧→新）。

    行格式：`<local-ref> SP <local-object-name> SP <remote-ref> SP <remote-object-name>`。
    ref 删除行（local-ref = "(delete)"，对象名为全零）不产生待推提交。
    返回 None 表示没有可用的行（回退默认模式）。
    """
    shas: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts[0], parts[1], parts[2], parts[3]
        if set(local_sha) == {"0"}:  # 删除远端引用
            continue
        if set(remote_sha) == {"0"}:  # 新分支：所有不在远端的提交
            shas += run_git("rev-list", "--reverse", local_sha, "--not", "--remotes=origin").split()
        else:
            shas += run_git("rev-list", "--reverse", f"{remote_sha}..{local_sha}").split()
    return shas if shas else None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    argv = [a for a in sys.argv[1:] if a not in ("--allow", "-a")]
    allow = ("--allow" in sys.argv[1:] or "-a" in sys.argv[1:]) or os.environ.get("PUSH_GUARD_ALLOW", "") not in ("", "0")

    try:
        shas = resolve_batch(argv)
    except RuntimeError as exc:
        print(f"[pre-push-guard] 环境错误：{exc}", file=sys.stderr)
        return 2

    if not shas:
        print("[pre-push-guard] 无待推送提交，放行。")
        return 0

    failures: list[str] = []
    warnings: list[str] = []
    subjects: dict[str, str] = {}
    rows: list[str] = []

    for sha in shas:
        short = sha[:9]
        files, subject, is_merge = commit_info(sha)
        if is_merge:
            rows.append(f"  {short}  (merge, 跳过)  {subject[:60]}")
            continue
        rows.append(f"  {short}  {len(files)} 文件  {subject[:60]}")

        # R4 完全相同的标题
        if subject in subjects:
            failures.append(
                f"R4 {short} 与 {subjects[subject][:9]} 标题完全相同 —— 疑似信息文件串台：{subject[:60]}"
            )
        else:
            subjects[subject] = sha

        m = SUBJECT_RE.match(subject)
        if not m:
            failures.append(
                f"R1 {short} 标题不符合 `type(scope): 摘要` 形式：{subject[:60]}\n"
                f"   → 仓库约定用 git commit -F <独立信息文件>；并行会话严禁共用 .tools/commit-msg.txt"
            )
            continue
        scope_raw = m.group(2)
        tokens = [t.strip().lower() for t in re.split(r"[+&/、,]", scope_raw) if t.strip()]

        unknown = [t for t in tokens if t not in SCOPE_PATHS]
        if unknown:
            warnings.append(f"scope `{','.join(unknown)}` 不在 tools/pre-push-guard.py 的映射表里（{short}），请补登记")

        allowed = set(NEUTRAL_PREFIXES)
        for t in tokens:
            allowed.update(SCOPE_PATHS.get(t, []))

        # R2 越界文件
        offenders = [
            f for f in files
            if not any(f == p or f.startswith(p) for p in allowed)
        ]
        if offenders:
            show = "\n   ".join(offenders[:8]) + ("\n   …" if len(offenders) > 8 else "")
            failures.append(
                f"R2 {short} 有改动文件超出 scope `{scope_raw}` 声明的路径 —— "
                f"信息与代码不匹配，疑似顶包：\n   {show}"
            )

        # R3 每个 scope token 都要有文件命中
        for t in tokens:
            prefixes = SCOPE_PATHS.get(t, [])
            if prefixes and not any(
                f == p or f.startswith(p)
                for f in files
                for p in prefixes
            ):
                failures.append(f"R3 {short} scope `{t}` 没有任何改动文件与之匹配（改动：{len(files)} 个文件）")

    print("[pre-push-guard] 将推送的提交：")
    print("\n".join(rows))
    for w in warnings:
        print(f"[pre-push-guard][WARN] {w}")

    if failures:
        print("\n[pre-push-guard] 拦截本次 push（逐条核对将推送的提交信息与代码是否属于同一功能）：")
        for f in failures:
            print(f"  ✗ {f}")
        if allow:
            print("[pre-push-guard] PUSH_GUARD_ALLOW/--allow 人工放行 —— 请确认这是有意的跨切面提交。")
            return 0
        print(
            "\n  处理办法：\n"
            "  1) 逐条核对 git show <sha> —— 信息与代码不符的提交先 amend 改正（用独立的信息文件）；\n"
            "  2) 确属跨切面的合法提交：PUSH_GUARD_ALLOW=1 git push … 或 git push --no-verify；\n"
            "  3) 新 scope 请登记进 tools/pre-push-guard.py 的 SCOPE_PATHS。"
        )
        return 1

    print("[pre-push-guard] 通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
