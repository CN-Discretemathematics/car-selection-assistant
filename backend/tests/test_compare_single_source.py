"""对比选择状态必须**只有一份真值**（localStorage），按钮不得自建第二份。

## 缺陷（2026-10-03 修）

`AddToCompareButton` 原先是 `useState<boolean | null>(null)`，且**只在自己的
`toggle` 里**更新——既不在挂载时读 localStorage，也不订阅 `compare-changed`。
于是有两个可复现的错误：

1. 带已选状态进入页面时，按钮显示「加入对比」，与底部对比栏的「已选 N 个」当场矛盾；
2. 从对比栏移除该款型后按钮仍显示「已加入对比」+ 对勾；此时再点它，
   `readCompareIds()` 里已无该 id，于是走「不在里面 → 重新加入」分支——
   **文案说移除、行为是加回**，用户点几次都删不掉。

## 为什么用源码级断言

前端没有 React 测试框架（`pnpm test` 跑 `lib/**/*.test.mts` 的纯逻辑）。
本仓既有先例 `test_detail_entry_contract.py` 已确立「最小静态断言兜住表现层」。

这类「状态被复制成两份」的缺陷，`tsc` 与 eslint 都发现不了：它类型正确、
无未使用变量，只是**行为**与另一处真值源不一致。唯一能守住的办法是把
「必须订阅 + 必须挂载即读 + 不得用 null 初值」写成契约。
"""
from __future__ import annotations

import re
from pathlib import Path


def _strip_comments(src: str) -> str:
    """去掉注释后再做源码级断言。

    这不是洁癖：写这个测试时它当场抓到了自己——我在修 H9 的文档注释里写了
    `useState<boolean | null>`（描述旧缺陷的写法），于是「不得再出现 null 初值」
    这条断言被**注释本身**触发。契约测试若会被注释破坏，就等于给「把说明改成
    旧代码的样子」开了一个假红灯的入口。先剥注释，断言才只对代码负责。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)      # 块注释
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)      # 整行注释
    return src


_WEB = Path(__file__).resolve().parents[2] / "web" / "app" / "components"
_BUTTON = _strip_comments((_WEB / "AddToCompareButton.tsx").read_text(encoding="utf-8"))
_BAR = _strip_comments((_WEB / "CompareBar.tsx").read_text(encoding="utf-8"))


def test_event_name_is_exported_not_duplicated():
    """事件名必须是 CompareBar 导出的单一常量，不得在按钮里再抄一份字面量。"""
    assert re.search(r"export const COMPARE_EVENT = ", _BAR), "COMPARE_EVENT 应由 CompareBar 导出"
    assert "COMPARE_EVENT" in _BUTTON, "按钮必须 import 这个常量"
    assert not re.search(r'addEventListener\("compare-changed"', _BUTTON), (
        "按钮里不得硬编码事件名字面量——那会与 CompareBar 的常量漂移"
    )


def test_button_subscribes_to_compare_changes():
    """按钮必须订阅 compare-changed 与 storage，并在挂载时立刻同步一次。"""
    for token in ('addEventListener(COMPARE_EVENT', 'addEventListener("storage"',
                  "removeEventListener(COMPARE_EVENT", 'removeEventListener("storage"'):
        assert token in _BUTTON, f"按钮缺少 {token}（跨组件状态不同步的根因）"
    # 挂载即读：effect 内第一次调用不能是纯注册
    effect = _BUTTON.split("useEffect(() => {", 1)[1].split("}, [variantId]);", 1)[0]
    assert re.search(r"const sync = \(\) => setSelected\(readCompareIds\(\)", effect), (
        "sync 必须从 localStorage 读真值，而不是从本地 state 推导"
    )
    assert effect.strip().splitlines()[1].strip() == "sync();", "effect 内必须先同步一次再注册监听"


def test_button_has_no_null_initial_state():
    """null 初值意味着「尚未加载」与「未选中」不可区分，SSR 首屏也拿不到 aria-pressed。"""
    assert "useState<boolean | null>" not in _BUTTON, "不得用 null 初值（正是旧缺陷的写法）"
    assert "useState(false)" in _BUTTON
    # aria-pressed 必须始终反映真值，而不是首次点击前缺席
    assert "aria-pressed={selected ?? undefined}" not in _BUTTON
    assert "aria-pressed={selected}" in _BUTTON


def test_compare_bar_keeps_its_own_sync_contract():
    """对照实现本身不能被改坏（它是这个契约的参照物）。"""
    bar_effect = _BAR.split("useEffect(() => {", 1)[1].split("}, []);", 1)[0]
    for token in ('addEventListener(COMPARE_EVENT', 'addEventListener("storage"',
                  "setIds(readCompareIds())"):
        assert token in bar_effect, f"CompareBar 丢失了 {token}（参照实现被改坏）"
