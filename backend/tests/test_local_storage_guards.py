"""localStorage 的**读与写都要有保护**，且写失败必须如实上报（2026-10-03 修）。

## 缺陷：只防读、不防写的不对称

`CompareBar.readCompareIds()` 既有 SSR 守卫（`typeof window === "undefined"`）
又有 try/catch；`writeCompareIds()` 却是裸 `window.localStorage.setItem`。
在 Safari 无痕模式 / 用户禁用 Cookie / 配额满时，`setItem` 会**抛异常**，
于是点「加入对比」抛未捕获错误、按钮纹丝不动——看起来像坏了。

更糟的是它此前**返回 void**，三个调用方都无法判断写没写进去：

- `AddToCompareButton.toggle` 无条件 `setSelected(true)`；
- `CompareBar.clear` 无条件当作成功；
- `AgentChat` 的「加入对比」按钮无条件 `setAdded(true)` → 按钮显示「已加入」，
  对比栏里却没有这一台。**文案说加入了、实际没有**，比不响应更难排查。

## 修法

`writeCompareIds` 改为返回 `boolean`，`setItem` 包 try/catch；**写失败时不派发
事件**（没改成却广播，等于告诉所有订阅者「已经变了」）。三个调用方都据返回值
决定是否更新本地状态，失败时如实提示。写法对齐 `AgentChat.tsx:70-74`——
那是同仓早就写对的读/写保护，本次只是**写路径**漏了。
"""
from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web" / "app" / "components"
_BAR = (_WEB / "CompareBar.tsx").read_text(encoding="utf-8")
_BAR_CODE = re.sub(r"/\*.*?\*/", "", _BAR, flags=re.S)
_BAR_CODE = re.sub(r"^\s*//.*$", "", _BAR_CODE, flags=re.M)
_BUTTON = (_WEB / "AddToCompareButton.tsx").read_text(encoding="utf-8")
_CHAT = (_WEB / "AgentChat.tsx").read_text(encoding="utf-8")


def _fn_body(src: str, name: str) -> str:
    m = re.search(r"export function " + name + r"\((.+?)\n\}", src, re.S)
    assert m, f"找不到 {name}"
    return m.group(0)


def test_read_path_keeps_ssr_guard_and_try():
    read = _fn_body(_BAR_CODE, "readCompareIds")
    assert 'if (typeof window === "undefined") return [];' in read, "读路径的 SSR 守卫不能丢"
    assert "try {" in read and "catch" in read, "读路径需容错（坏 JSON / 存储不可用）"


def test_write_path_is_guarded_and_returns_boolean():
    write = _fn_body(_BAR_CODE, "writeCompareIds")
    assert "try {" in write and "catch" in write, (
        "写路径必须 try/catch——无痕模式 setItem 会抛，未捕获会让按钮毫无反应"
    )
    assert ": boolean" in write, "writeCompareIds 必须返回是否真的写进去了"
    # 返回值语义：成功派发事件、失败不派发
    assert re.search(r"catch \{\s*return false;", write), "失败分支必须 return false"
    dispatch = write.index("dispatchEvent")
    ret_false = write.index("return false")
    assert ret_false < dispatch, "写失败时不得派发事件（没改成却广播 = 告诉订阅者已经变了）"


def test_all_callers_branch_on_the_result():
    """三个调用方都必须据返回值决定本地状态，否则「已加入」仍是假的。"""
    for name, src in (("AddToCompareButton", _BUTTON), ("AgentChat", _CHAT)):
        assert re.search(r"if \(!writeCompareIds\(", src), (
            f"{name} 必须检查写入结果"
        )
        # 检查结果的那次调用之后，不得无条件把本地状态置为「已加入」
        for m in re.finditer(r"writeCompareIds\(([^\n]*)\);\n(\s*)", src):
            after = src[m.end() : m.end() + 80]
            assert "setAdded(true)" not in after and "setSelected(" not in after, (
                f"{name} 仍会在写入后无条件更新本地状态"
            )


def test_compare_bar_clear_reports_failure():
    clear = re.search(r"const clear = useCallback\(\(\) => \{(.+?)\}, \[\]\);", _BAR_CODE, re.S)
    assert clear, "找不到 clear"
    assert "if (!writeCompareIds([]))" in clear.group(1), "清空也必须如实上报失败"
