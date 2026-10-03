"""pre-push-guard 的豁免**必须逐提交匹配**（2026-10-03 修 + 回归钉住）。

## 事故经过

旧实现把失败记成一条**范围级**总账：范围内**任意一笔**提交带 `# gate-allow:`
trailer，就整段 `return 0`。于是：

    # 手动跑（含那笔带 trailer 的提交）        → 通过
    git push → hook 判「待推送子范围」（不含）  → 拦截

**本地绿、push 红，两边输出看不出差别。** 本会话实际踩到过：本地验证让我以为门禁
通过，push 被拦，还一度误判成「网络问题」。

一笔合法的跨切面豁免，会顺带放行范围内**其它提交的真实越界**——那正是这道门禁
要拦的东西。

## 本文件钉住的行为

用临时仓库造三笔提交，逐个范围验证：

- 只有无豁免的越界提交 → 拦
- 无豁免的越界 + **有豁免的越界** → **仍然拦**（关键：豁免不顺带生效）
- 只有有豁免的越界提交 → 放行（豁免对它自己生效）
- 干净提交 → 放行
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ⚠️ 门禁在**仓库根**的 tools/ 下，不在 backend/tools/：
# __file__ = backend/tests/… → parents[0]=tests, [1]=backend, **[2]=仓库根**。
# 写成 parents[1] 会拿到 backend/tools/pre-push-guard.py（不存在），
# subprocess 直接以 exit 2 失败——四条用例一起红，差点以为门禁改坏了。
GUARD = Path(__file__).resolve().parents[2] / "tools" / "pre-push-guard.py"


def git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    assert p.returncode == 0, f"git {args} 失败: {p.stderr[:200]}"
    return p.stdout.strip()


def run_guard(cwd: Path, rng: str) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, str(GUARD), rng], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return p.returncode, p.stdout


def build() -> tuple[tempfile.TemporaryDirectory, Path, dict[str, str]]:
    td = tempfile.TemporaryDirectory()
    root = Path(td.name) / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.name", "t", cwd=root)
    git("config", "user.email", "t@e", cwd=root)
    (root / "docs").mkdir()
    (root / "docs" / "seed.md").write_text("seed\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "chore(init): base", cwd=root)
    base = git("rev-parse", "HEAD", cwd=root)

    def commit(name: str, subject: str, trailer: str = "") -> str:
        (root / "docs" / f"{name}.md").write_text(f"{name}\n", encoding="utf-8")
        git("add", "-A", cwd=root)
        body = subject + ("\n\n" + trailer if trailer else "") + "\n"
        # 信息文件必须写在**仓库外**——写在仓库里会被 `git add -A` 扫进提交，
        # 每笔都凭空多个越界文件（第一版就栽在这，还一度以为门禁改坏了）。
        msg = root.parent / f"msg-{name}.txt"
        msg.write_text(body, encoding="utf-8")
        git("commit", "-q", "-F", str(msg), cwd=root)
        msg.unlink()
        return git("rev-parse", "HEAD", cwd=root)

    a1 = commit("a1", "fix(agent): 改了 docs/ 却声明 agent scope")   # 越界，无豁免
    a2 = commit("a2", "fix(agent): 同样越界但带豁免",
                "# gate-allow: 测试用：这一笔确实是跨切面")
    a3 = commit("a3", "docs(sync): 干净提交")                        # 不越界
    return td, root, {"base": base, "a1": a1, "a2": a2, "a3": a3}


@pytest.fixture()
def repo():
    td, root, shas = build()
    yield root, shas
    td.cleanup()


def test_violation_without_waiver_is_blocked(repo):
    root, s = repo
    code, out = run_guard(root, f"{s['base']}..{s['a1']}")
    assert code == 1, f"无豁免的越界提交必须被拦，实际 {code}：{out[-300:]}"


def test_waiver_does_not_carry_over_to_other_commits(repo):
    """**本文件的核心判据**：一笔的豁免不得放行同范围内其它提交的越界。"""
    root, s = repo
    code, out = run_guard(root, f"{s['base']}..{s['a2']}")
    assert code == 1, (
        "❗范围级豁免仍在生效：a2 的 trailer 放行了 a1 的真实越界。"
        f"实际 {code}：{out[-300:]}"
    )
    # a2 自己的豁免应当被明确记录，而不是悄悄放行
    assert "已按提交逐一放行" in out, f"输出未说明逐条豁免：{out[-300:]}"


def test_waiver_applies_to_its_own_commit(repo):
    root, s = repo
    code, out = run_guard(root, f"{s['a1']}..{s['a2']}")
    assert code == 0, f"携带自己 trailer 的提交应被豁免，实际 {code}：{out[-300:]}"
    assert "gate-allow" in out, "豁免理由应被打印出来以便审计"


def test_clean_commit_passes(repo):
    root, s = repo
    code, out = run_guard(root, f"{s['a2']}..{s['a3']}")
    assert code == 0, f"干净提交应通过，实际 {code}：{out[-300:]}"
