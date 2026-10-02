"""全站错误边界必须存在、且不把内部信息端给用户（2026-10-03 新增时钉住）。

## 为什么需要这个文件

`web/app` 下此前**没有任何** error/loading/not-found 文件，于是任何服务端渲染
出错都落到框架自带的通用错误页：站点外壳（页脚、对话入口、对比栏）全部消失，
只剩一句技术性提示，用户**没有任何可点的恢复入口**。这不是"体验不好"，是
把一次可恢复的失败变成了一条死路。

## 两条容易破的约束

1. **根级放置**。放在 `app/error.tsx` 而非某个子路由下，才兜得住所有页面；且
   `RootLayout` 渲染的是 `{children}` + 页脚，边界只替换 children，**外壳保留**。
   挪到子目录会让一半页面失去这个边界。
2. **不渲染 `error.message`**。它可能含内部路径、键名、上游报错原文——是给
   排障看的。允许出现的只有 `error.digest`：框架生成的**不透明**编号，不泄露
   实现细节，但能让用户报障时对上服务端日志。

本仓前端没有 React 测试框架，故沿用源码级契约（先例：`test_detail_entry_contract.py`）。
`next build` 会对 error boundary 的合法性与类型做真实校验，本文件守的是
「构建管不到、但会被后人无意改掉」的那部分。
"""
from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "web" / "app"
_ERROR = _APP / "error.tsx"
_SRC = _ERROR.read_text(encoding="utf-8")
_CODE = re.sub(r"/\*.*?\*/", "", _SRC, flags=re.S)
_CODE = re.sub(r"^\s*//.*$", "", _CODE, flags=re.M)


def test_error_boundary_exists_at_root_segment():
    assert _ERROR.exists(), "app/error.tsx 缺失：渲染出错会掉进框架通用错误页"
    assert not list(_APP.rglob("*/error.tsx")), (
        "子路由下的 error.tsx 存在——请确认根级边界仍覆盖全部页面"
    )


def test_is_client_component_with_standard_props():
    assert _CODE.lstrip().startswith('"use client";'), "错误边界必须是客户端组件"
    assert re.search(r"error:\s*Error\s*&\s*\{\s*digest\?:\s*string\s*\}", _CODE), (
        "props 需声明 error 的 digest 字段"
    )
    assert re.search(r"reset:\s*\(\)\s*=>\s*void", _CODE), "props 需声明 reset"
    assert re.search(r"export default function", _CODE), "需默认导出"


def test_never_renders_error_message():
    """绝不能把 error.message 渲染给用户——它可能含内部路径/键名/上游报错原文。"""
    for m in re.finditer(r"\{error\.(\w+)\}", _CODE):
        assert m.group(1) == "digest", (
            f"页面渲染了 error.{m.group(1)}，只有 digest 是不透明编号、可以给用户看"
        )
    assert "error.message" not in _CODE
    assert "error.stack" not in _CODE


def test_failure_still_leaves_a_trace():
    """失败要显式：控制台必须留痕，否则边界就成了静默吞错的黑洞。"""
    assert re.search(r"useEffect\(\(\)\s*=>\s*\{\s*console\.error\(", _CODE), (
        "错误边界应把错误打到控制台（只留痕、不上页面）"
    )
    assert re.search(r"\}, \[error\]\);", _CODE), "useEffect 依赖应为 error"


def test_offers_recovery_and_a_way_home():
    assert re.search(r"onClick=\{reset\}", _CODE), "必须提供重试（reset）入口"
    assert 'href="/"' in _CODE, "必须提供返回首页的出口"
    assert 'from "next/link"' in _CODE, "回首页应走 next/link，不要裸 <a>"


def test_digest_only_rendered_when_present():
    """digest 是可选字段，缺失时不能渲染出「问题编号：」这种空壳。"""
    assert re.search(r"\{error\.digest\s*\?", _CODE), "digest 应为条件渲染（存在时才显示）"


def test_page_is_reachable_by_assistive_tech():
    """错误页也应能被读屏播报（替换的是主内容区）。"""
    assert 'id="main-content"' in _CODE, "应复用布局的 main-content 锚点"
    assert 'role="alert"' in _CODE, "错误提示需可被读屏播报"
