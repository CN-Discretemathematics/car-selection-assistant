# -*- coding: utf-8 -*-
"""pre-push-guard 的 R4 在 **force-push** 场景下的范围（2026-10-23 修）

## 事故经过

force-push 一个 **rebase 过**的分支时，门禁把**别人已经合并、已经评审过**的
main 提交也拉进来核对，于是：

```
[pre-push-guard] 拦截本次 push：
  ✗ R4 7a5c1f73a 与 e4daecd0c 标题完全相同 —— 疑似信息文件串台
```

`e4daecd` 与 `7a5c1f7` 是**同一改动的两份**（rebase 前后各留了一份），
都已在 main 上、都已过 PR 评审。⇒ 一个完全正常的 force-push 被拦死，
且报错信息指向「串台」——**误导排查方向**（当时先怀疑是并行会话串台）。

## 根因

`_shas_from_push_lines` 在 force-push 时用 `remote_sha..local_sha`：

    远端旧基线 ──┬── 本地新基线（rebase 后的 main 侧）── 本地新提交
                 └── main 上的新提交  ← 全部被算进「待推送提交」

⇒ R4 在这一大堆**别人的**提交之间比对标题，自然会撞上 main 里既有的同标题副本。

## 本文件钉住的行为

- 核对范围只取「**这次推送真正新增**的提交」（排除 origin/main 上已有的）；
- R4 仍会抓住「**同一批新增提交**之间标题重复」——串台防护**没有被削弱**；
- 普通 push（远端已有该提交）不受影响。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "tools" / "pre-push-guard.py"


def git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    assert p.returncode == 0, f"git {args} 失败: {p.stderr[:300]}"
    return p.stdout.strip()


def run_guard(cwd: Path, rng: str | None = None) -> tuple[int, str]:
    args = [sys.executable, str(GUARD)]
    if rng:
        args.append(rng)
    p = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return p.returncode, p.stdout + p.stderr


def commit(root: Path, name: str, subject: str, rel: str | None = None) -> str:
    path = root / (rel or f"docs/{name}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{name}\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    msg = root.parent / f"msg-{name}.txt"
    msg.write_text(subject + "\n", encoding="utf-8")
    git("commit", "-q", "-F", str(msg), cwd=root)
    msg.unlink()
    return git("rev-parse", "HEAD", cwd=root)


def build_force_push_repo() -> tuple[tempfile.TemporaryDirectory, Path]:
    """造出事故现场：

    - `main` 上有两笔**同标题**的等价提交（rebase 前后的同一改动）；
    - 远端 `origin/main` 指向其中较新的那笔；
    - 待推分支基于 main，并有一笔新提交；
    - 远端该分支停在一个**旧基线**上（⇒ push 只能是 force-push）。
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name) / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.name", "t", cwd=root)
    git("config", "user.email", "t@e", cwd=root)
    (root / "docs").mkdir()

    base = commit(root, "seed", "chore(init): base")
    git("update-ref", "refs/remotes/origin/main", base, cwd=root)

    # 远端 feature 分支上**已有一笔旧提交**（模拟「这个分支以前推过别的内容」）。
    # ⚠️ 必须是**独立**的一笔（不是 base 的别名）——否则 `remote_sha..local_sha`
    # 的范围会退化，掩盖住「排除写得太宽」这类变异。
    git("checkout", "-q", "-b", "old-feature", cwd=root)
    commit(root, "old-remote", "chore(deploy): 远端分支上已有的一笔")
    old_remote = git("rev-parse", "HEAD", cwd=root)
    git("update-ref", "refs/remotes/origin/feature", old_remote, cwd=root)
    git("checkout", "-q", "main", cwd=root)

    # 同一改动的两份：rebase 前（a）与 rebase 后（b），标题完全相同
    commit(root, "dup-a", "fix(tools+test): 同一改动的第一份")
    commit(root, "dup-b", "fix(tools+test): 同一改动的第一份")
    main_tip = git("rev-parse", "HEAD", cwd=root)
    git("update-ref", "refs/remotes/origin/main", main_tip, cwd=root)

    # 待推分支：基于 main，加一笔新提交
    git("checkout", "-q", "-b", "feature", cwd=root)
    commit(root, "new", "chore(deploy): 本次真正新增的提交")
    return td, root


@pytest.fixture()
def repo():
    td, root = build_force_push_repo()
    yield root
    td.cleanup()


def test_force_push_does_not_check_commits_already_on_main(repo):
    """**核心判据**：force-push 时不得核对 origin/main 上已有的提交。

    这些提交已经过 PR 评审；把它们拉进来核对会把正常的 force-push 拦死，
    且报错信息误导为「串台」。
    """
    # 模拟 hook 的 stdin：<local-ref> <local-sha> <remote-ref> <remote-sha>
    local_sha = git("rev-parse", "HEAD", cwd=repo)
    remote_sha = git("rev-parse", "refs/remotes/origin/feature", cwd=repo)
    stdin = f"refs/heads/feature {local_sha} refs/heads/feature {remote_sha}\n"

    p = subprocess.run(
        [sys.executable, str(GUARD)], cwd=repo, input=stdin, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out = p.stdout + p.stderr
    assert p.returncode == 0, f"force-push 不该被拦，实际 {p.returncode}：{out[-500:]}"
    assert "R4" not in out, f"不该出现 R4 判定（main 上的同标题提交被误算了）：{out[-500:]}"
    # 但新增的那笔必须**确实被核对过**（不能因为排除而漏检）
    assert "本次真正新增的提交" in out, f"新增提交未被纳入核对：{out[-500:]}"


def test_force_push_still_checks_the_new_commit_scope(repo):
    """**新增提交的越界仍必须被拦**——排除 main 历史不得变成「放行一切」。

    这是上一条判据的另一半：上一条只证明「新增提交出现在输出里」，
    但如果排除写得太宽（把新增提交也排掉），那一条照样会红——
    本条直接验证**判定仍然生效**。
    """
    # 造一笔越界的提交：声明 agent scope，却改了 agent scope **管不到**的路径
    # （`backend/app/agent/` 属于 agent scope ⇒ 放在那里并不越界，
    #   必须挑一个真正落在 scope 之外的路径，否则这条用例恒真）
    git("checkout", "-q", "-b", "bad", cwd=repo)
    path = repo / "backend" / "app" / "vehicles" / "leak.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    msg = repo.parent / "msg-bad.txt"
    msg.write_text("fix(agent): 越界提交\n", encoding="utf-8")
    git("commit", "-q", "-F", str(msg), cwd=repo)
    msg.unlink()

    local_sha = git("rev-parse", "HEAD", cwd=repo)
    remote_sha = git("rev-parse", "refs/remotes/origin/feature", cwd=repo)
    stdin = f"refs/heads/bad {local_sha} refs/heads/bad {remote_sha}\n"

    p = subprocess.run(
        [sys.executable, str(GUARD)], cwd=repo, input=stdin, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out = p.stdout + p.stderr
    assert p.returncode == 1, (
        f"新增提交的越界必须被拦（排除 main 历史不得变成放行一切），实际 {p.returncode}：{out[-500:]}"
    )
    assert "leak.txt" in out, f"应指出越界文件：{out[-500:]}"


def test_r4_still_catches_duplicate_titles_among_new_commits(repo):
    """**R4 不得被削弱**：同一批**新增**提交之间标题重复，仍必须拦。

    这是串台防护本身——若因为修了 force-push 范围而放过它，门禁就形同虚设。
    """
    git("checkout", "-q", "-b", "dup-new", cwd=repo)
    commit(repo, "x1", "fix(agent): 同一标题的新增提交")
    commit(repo, "x2", "fix(agent): 同一标题的新增提交")
    local_sha = git("rev-parse", "HEAD", cwd=repo)
    remote_sha = git("rev-parse", "refs/remotes/origin/main", cwd=repo)
    stdin = f"refs/heads/dup-new {local_sha} refs/heads/dup-new {remote_sha}\n"

    p = subprocess.run(
        [sys.executable, str(GUARD)], cwd=repo, input=stdin, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out = p.stdout + p.stderr
    assert p.returncode == 1, f"新增提交之间标题重复必须被拦，实际 {p.returncode}：{out[-500:]}"
    assert "R4" in out, f"应报 R4：{out[-500:]}"


def test_normal_push_scope_is_unchanged(repo):
    """普通 push（远端已有旧提交）不受影响：该核的照核。"""
    code, out = run_guard(repo, "refs/remotes/origin/main..HEAD")
    assert code == 0, f"普通范围应通过，实际 {code}：{out[-300:]}"


def test_exclusion_is_scoped_to_origin_main_only(repo):
    """**排除范围必须恰好是 origin/main**——不是所有 origin 引用。

    为什么单独钉这一条：`--not --remotes=origin`（排除所有远端引用）在
    「远端只有 main」时与 `--not origin/main` **结果完全相同** ⇒ 前面那些
    端到端用例分不出两者，写错了也测不出来。

    而两者在真实仓库里**不等价**：远端还有别的分支（`origin/<别的分支>`），
    那些分支上的提交若恰好在本次推送范围内，用 `--remotes=origin` 就会被
    漏核（= 越界提交逃过门禁）。

    ⇒ 这里直接读出实现里的排除参数，钉死「只排除 origin/main」。
    """
    src = GUARD.read_text(encoding="utf-8")
    # 实现里这两个参数可能被换行拆开（格式化成多行）⇒ 用正则跨空白匹配。
    #
    # ⚠️ 只看 **force-push 那条路径**：`--remotes=origin` 在**新分支**路径里是
    # 原有且正确的（新建分支时排除所有远端引用 = 正确语义），
    # 所以不能全文件禁掉它——必须定位到 force-push 的分支里判断。
    m = re.search(
        r"else:\s*\n\s*#.*\n\s*shas \+= run_git\(\s*\n"
        r"\s*\"rev-list\", \"--reverse\", f\"\{remote_sha\}\.\.\{local_sha\}\",\s*"
        r"\"--not\",\s*(\"[^\"]+\")",
        src,
    )
    assert m, "未能定位 force-push 那条路径的 rev-list 调用（实现结构变了？）"
    assert m.group(1) == '"origin/main"', (
        f"force-push 的排除范围必须恰好是 origin/main，实际 {m.group(1)}"
        "（排除所有 origin 引用会漏核远端别的分支上的提交 ⇒ 越界提交逃过门禁）"
    )
