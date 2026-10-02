"""ops/rag 页面的两个前端缺陷的**源码级契约**测试（H8 / H9，2026-10-03 修）。

## 为什么用源码级断言而不是组件测试

本仓前端没有 React 测试框架（`pnpm test` 跑的是 `lib/**/*.test.mts` 的纯逻辑，
用的是 Node 内置 runner）。`tests/test_detail_entry_contract.py` 已确立了
「用最小静态断言兜住表现层」的先例，本文件沿用同一手法。

## 两个缺陷（审计记录与实际机制均有出入，如实更正）

**H9｜3/4 个 tab 加载失败后永久「加载中…」，无重试入口** —— 与审计一致。
GraphTab / StatusTab / RunsTab 都是 `fetchX().then(setX).catch(onError)` +
`if (!x) return 加载中…`：失败时只往父组件塞一条红字，**自身 state 仍是 null**，
于是正文永远停在「加载中…」，且页面上没有任何重试入口。EvalTab 是唯一做对的。

**H8｜审计写的是「轮询 interval 在 unmount 泄漏」，实际机制不同**：
清理函数 `useEffect(() => () => clearInterval(pollRef.current), [])` **本来就在**。
「重复点击导致孤儿 interval」也不可达——按钮在 `busy !== null` 期间是 disabled 的。
真正的泄漏是**竞态**：点「dense 重建」后立刻离开页面，unmount 清理跑完时
`pollRef.current` 还是 null（interval 要等 `runReindex` 的网络往返 resolve 之后
才被创建），随后续段仍会 setInterval —— 这个 interval 再也没有任何人会清它，
每 2.5s 打一次后端直到页面关闭。
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "web" / "app" / "ops" / "rag" / "page.tsx"
_SOURCE = _SRC.read_text(encoding="utf-8")


def _tab_blocks() -> dict[str, str]:
    """按顶层 `function XxxTab(` 切出 4 个 tab 的源码块。"""
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"^function (\w+Tab)\(", _SOURCE, re.M)]
    blocks = {}
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(_SOURCE)
        blocks[name] = _SOURCE[pos:end]
    return blocks


def test_four_tabs_exist():
    blocks = _tab_blocks()
    assert set(blocks) >= {"GraphTab", "StatusTab", "RunsTab", "EvalTab"}


def test_every_data_tab_uses_the_shared_hook():
    """4 个数据 tab 必须走 useAsyncData，不得再各自 fetch-then-set。"""
    blocks = _tab_blocks()
    for name in ("GraphTab", "StatusTab", "RunsTab", "EvalTab"):
        block = blocks[name]
        assert "useAsyncData(" in block, f"{name} 未使用共享的 useAsyncData（H9 回归）"
        assert not re.search(r"\.then\(set\w+\)", block), (
            f"{name} 仍在手写 fetch().then(setX)——那正是 H9 的成因"
        )


def test_loading_state_always_has_an_error_branch_with_retry():
    """凡是渲染「加载中…」的 tab，必须先判 error 并给出重试入口。

    这是 H9 的核心不变式：只写 `if (!x) return 加载中…` 而没有 error 分支的代码，
    加载失败时就会永久停在该文案上。
    """
    blocks = _tab_blocks()
    checked = 0
    for name, block in blocks.items():
        if "加载中…" not in block:
            continue
        checked += 1
        assert "if (error)" in block, f"{name} 渲染了「加载中…」却没有 error 分支（H9 回归）"
        assert "reload" in block, f"{name} 的失败分支没有重试入口（H9 回归）"
    assert checked >= 4, f"应至少检查 4 个渲染「加载中…」的 tab，实际 {checked}"


def test_shared_load_error_offers_retry():
    assert "function LoadError(" in _SOURCE
    body = _SOURCE.split("function LoadError(", 1)[1][:600]
    assert "onRetry" in body, "LoadError 必须真的提供重试按钮，不能只显示文案"
    assert "重试" in body


def test_polling_cannot_outlive_unmount():
    """H8：unmount 之后到达的 await 续段不得创建定时器。

    三处都要在：runReindex 之后、轮询回调首行、以及创建前清掉旧 interval。
    """
    block = _tab_blocks()["StatusTab"]
    assert "mountedRef" in block, "StatusTab 必须有 unmount 守卫（H8 回归）"
    # await 之后紧跟的守卫：防止「离开页面后仍起轮询」
    assert re.search(
        r"await runReindex\(token, target\);\s*\n\s*if \(!mountedRef\.current\) return;",
        block,
    ), "runReindex 之后必须先判 mountedRef 再继续（H8：interval 在 await 之后才创建）"
    # 轮询回调自身也要判（组件可能在轮询期间被卸载）
    assert re.search(
        r"setInterval\(async \(\) => \{\s*\n\s*if \(!mountedRef\.current\)",
        block,
    ), "轮询回调首行必须判 mountedRef（H8 回归）"
    # 创建前清掉可能存在的旧 interval
    assert re.search(
        r"if \(pollRef\.current\) window\.clearInterval\(pollRef\.current\);\s*\n\s*pollRef\.current = window\.setInterval",
        block,
    ), "创建新 interval 前必须先清旧的"
    # 卸载时置位并清空
    assert re.search(r"mountedRef\.current = false;", block), "卸载时必须把 mountedRef 置 false"
