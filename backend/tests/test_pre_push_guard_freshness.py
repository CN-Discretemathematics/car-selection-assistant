"""`check_base_freshness`（pre-push 门禁 R5）的测试。

## 为什么门禁自己也要测

R5 是「推送前基线必须是最新 origin/main」（2026-10-05 用户定规，见 AGENTS.md）。
它**只写进文档是不够的**——实测两次踩坑都发生在「我记得我 rebase 了」的自信上：

- #60 未 rebase 就推 → GitHub 报 `mergeable_state=dirty`，评审打开就是冲突；
- #56/#57 的 base 设成别的分支 → GitHub 显示 `merged`，但内容压根没进 main。

规则被机械执行才有意义。而门禁自己若没被测，就会像 #56/#57 那样——
**看起来在管事，实际某条路径上没生效**。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_guard():
    """按路径加载 `pre-push-guard.py`——文件名带连字符，不能直接 import。"""
    spec = importlib.util.spec_from_file_location(
        "pre_push_guard", ROOT / "tools" / "pre-push-guard.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["pre_push_guard"] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _fake_git(counts: dict[tuple[str, ...], str]):
    """把 run_git 换成按参数查表的假实现。"""

    def _run(*args: str) -> str:
        return counts.get(tuple(args), "")

    return _run


def test_up_to_date_is_not_stale(monkeypatch):
    """基线最新（本分支领先 main）→ 放行。"""
    monkeypatch.setattr(
        guard,
        "run_git",
        _fake_git({
            ("rev-list", "--count", "HEAD..origin/main"): "0",
            ("rev-list", "--count", "origin/main..HEAD"): "3",
        }),
    )

    stale, detail = guard.check_base_freshness()

    assert stale is False
    assert "基线最新" in detail


def test_behind_main_is_stale(monkeypatch):
    """main 领先 → 必须拦，并报出双方各领先几笔。"""
    monkeypatch.setattr(
        guard,
        "run_git",
        _fake_git({
            ("rev-list", "--count", "HEAD..origin/main"): "7",
            ("rev-list", "--count", "origin/main..HEAD"): "1",
        }),
    )

    stale, detail = guard.check_base_freshness()

    assert stale is True
    assert "7" in detail and "1" in detail


def test_offline_does_not_block(monkeypatch):
    """拿不到 origin/main 时**放行**——离线不该把开发卡死。"""
    monkeypatch.setattr(guard, "run_git", _fake_git({}))

    stale, detail = guard.check_base_freshness()

    assert stale is False
    assert "跳过" in detail


def test_r5_is_wired_into_main(monkeypatch, capsys):
    """R5 必须真的接在 main() 里。

    这条是**防「门禁写了但没接线」**的用例——#56/#57 就是这么丢的内容：
    规则存在于文档/PR 描述里，但没被执行，于是没人知道它该跑。
    """
    monkeypatch.setattr(guard, "run_git", _fake_git({
        ("rev-list", "--count", "HEAD..origin/main"): "5",
        ("rev-list", "--count", "origin/main..HEAD"): "1",
        ("rev-list", "--reverse", "--no-walk"): "deadbeef123",
    }))
    monkeypatch.setattr(guard, "commit_info", lambda sha: ([], "docs(x): y", False))
    monkeypatch.setattr(guard, "resolve_batch", lambda args: ["deadbeef123"])

    assert guard.main() == 1, "R5 落后时 main() 必须返回 1"
    out = capsys.readouterr().out
    assert "R5" in out
    assert "rebase origin/main" in out, "拦截文案必须给出可执行的下一步"


def test_subject_type_whitelist_rejects_unknown_type():
    """type 白名单是**闭集**：写错 type 会被 R1 拦下。

    本轮实测踩到：我把 `chore(eval)` 改成 `sync(eval)` 想迁就 scope，
    忘了 `sync` 不是 type——而我前两轮只看门禁输出的**末尾三行**，
    正好把 R1 的报错看漏了。这里把白名单钉住，别再靠人眼扫。
    """
    assert guard.SUBJECT_RE.match("docs(eval): 摘要")
    assert guard.SUBJECT_RE.match("feat(agent): 摘要")
    assert not guard.SUBJECT_RE.match("sync(eval): 摘要"), "sync 不是合法 type"
    assert not guard.SUBJECT_RE.match("eval: 摘要"), "缺 (scope)"


def test_gates_and_eval_scopes_are_registered():
    """`gates` / `eval` 必须已登记，且覆盖各自真正会碰到的路径。

    2026-10-05 之前这两个 scope 都没登记，于是相关提交只能靠内联
    `# gate-allow:` trailer 放行——**连着两笔**。例外机制被用成了常态，
    门禁就退化成「默认拦、记得写 trailer」。

    这里把登记**钉成可测的**：将来若有人误删这两个 key，测试立刻红。
    """
    assert "gates" in guard.SCOPE_PATHS, "gates scope 未登记"
    assert "eval" in guard.SCOPE_PATHS, "eval scope 未登记"

    gates = guard.SCOPE_PATHS["gates"]
    for path in ("tools/pre-push-guard.py", ".githooks/", "AGENTS.md"):
        assert any(path in p for p in gates), f"gates 未覆盖 {path}：{gates}"

    ev = guard.SCOPE_PATHS["eval"]
    for path in ("backend/tools/eval_", "backend/eval/", "backend/tests/test_eval_"):
        assert any(path in p for p in ev), f"eval 未覆盖 {path}：{ev}"


def test_gates_scope_does_not_swallow_the_whole_tools_dir():
    """`gates` 必须**只**覆盖门禁自身，不能顺手把整个 `tools/` 划进来。

    否则 `gates(scope)` 就成了万能钥匙：改 `tools/eval_rag.py` 也能用
    `gates` 标签通过，scope↔路径的对应关系失去意义。
    """
    gates = guard.SCOPE_PATHS["gates"]
    assert not any(p == "tools/" for p in gates), (
        "gates 不得覆盖整个 tools/——那会让 scope 形同虚设"
    )
    assert not any(p == "backend/tests/" for p in gates)


def test_known_scope_tokens_still_resolve():
    """回归：新增两个 key 之后，原有 scope 一个都不能少。"""
    for token in ("web", "ui", "compare", "facts", "vehicles", "agent", "rag",
                  "data", "security", "ops", "reviewer", "skills", "docs",
                  "lint", "hygiene", "chore", "test", "tools", "common"):
        assert token in guard.SCOPE_PATHS, f"{token} scope 丢失"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
