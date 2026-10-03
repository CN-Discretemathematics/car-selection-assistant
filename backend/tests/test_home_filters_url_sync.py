"""筛选表单必须跟随 URL，不能只在 mount 时读一次（2026-10-03 修）。

## 缺陷

`HomeFilters` 原先是 `useState({ … searchParams.get(…) })`。`useState` 的初始化器
**只在挂载时执行一次**，而 `useSearchParams()` 的值会随 URL 变化。于是：

- 浏览器**后退**后，地址栏已回到「无筛选」，表单却还显示「纯电」；用户看着表单
  点「确定」，就把刚刚退掉的筛选**重新加上**；
- 同一标签页打开带筛选参数的新链接时同理。

这与 `AddToCompareButton` 是同一类病：**把本该从真值源派生的状态缓存了下来**。
两处都 `tsc` 通过、eslint 无告警、类型完全正确——错的只是行为。

## 修法

对齐同仓已有的正确实现 `SearchBar.tsx:60-64`：把 URL 值提成 **primitive**，
再用 `useEffect` 跟随。之所以不直接 `useEffect(…, [searchParams])`：该对象每次
渲染都会换引用，会导致 effect 反复触发。

## 写这个测试时踩的坑（值得留着）

第一版把正则写进 f-string：`rf"const \\w+ = searchParams\\.get\\(\"{p}\""`。
这串里同时有 `\\(` 与 `\\"`，转义层数极易数错——我少写了一个闭合引号，于是
**断言对着完全正确的代码报红**。断言对正确代码报红比没有断言更糟：它会诱导
去改本来没问题的代码。所以这里的模式一律用字符串拼接，不进 f-string。
"""
from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web" / "app" / "components"
_FILTERS = (_WEB / "HomeFilters.tsx").read_text(encoding="utf-8")
_SEARCHBAR = (_WEB / "SearchBar.tsx").read_text(encoding="utf-8")

_PARAMS = ("energy_type", "body_type", "brand_type", "price_min", "price_max", "sort")
_FIELDS = ("energy_type", "body_type", "brand_type", "price_min", "price_max", "sort")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def test_url_values_are_lifted_to_primitives():
    """每个筛选参数都必须提成 primitive 变量——这是 effect 能正确依赖的前提。"""
    code = _strip_comments(_FILTERS)
    for p in _PARAMS:
        needle = 'const \\w+ = searchParams\\.get\\("' + p + '"'
        assert re.search(needle, code), (
            f"{p} 没有被提成 primitive；直接依赖 searchParams 对象会因引用变化反复触发"
        )


def test_form_resyncs_when_url_changes():
    """表单必须有跟随 URL 的 setForm，且依赖是那 6 个 primitive。"""
    code = _strip_comments(_FILTERS)
    effect = re.search(
        "useEffect\\(\\(\\) => \\{\\s*setForm\\(\\{(.+?)\\}\\);\\s*\\}, \\[(.+?)\\]\\);",
        code,
        re.S,
    )
    assert effect, "找不到「跟随 URL 重新播种表单」的 useEffect"
    deps = [d.strip() for d in effect.group(2).split(",")]
    assert len(deps) == len(_PARAMS), f"依赖应覆盖 6 个筛选参数，实际 {deps}"
    assert "searchParams" not in deps, "不得依赖 searchParams 对象本身（引用每次渲染都变）"
    for key in _FIELDS:
        # 接受 `key: value` 与 ES6 属性简写 `key,` 两种写法——字段在不在才是契约，
        # 用哪种语法不是。断言过死会让人为了迁就测试去改本来正确的代码。
        assert re.search("\\b" + key + "\\s*[:,]", effect.group(1)), f"重新播种时漏了 {key}"


def test_initial_state_still_reads_search_params():
    """SSR 首屏仍需从 URL 取初值，不能为了修 resync 把初值删掉。"""
    code = _strip_comments(_FILTERS)
    init = re.search("useState\\(\\{(.+?)\\}\\);", code, re.S)
    assert init, "找不到 useState 初值"
    for key in _FIELDS:
        assert key in init.group(1), f"初值缺 {key}"


def test_reference_implementation_still_correct():
    """SearchBar 是这个契约的参照实现，不能被改坏。"""
    needle = (
        'const urlKeyword = searchParams\\.get\\("q"\\) \\?\\? "";'
        "\\s*\\n\\s*useEffect\\(\\(\\) => \\{\\s*\\n\\s*setValue\\(urlKeyword\\);"
    )
    assert re.search(needle, _SEARCHBAR), (
        "SearchBar 的「跟随 URL 同步输入框」实现丢失了（参照物被改坏）"
    )
