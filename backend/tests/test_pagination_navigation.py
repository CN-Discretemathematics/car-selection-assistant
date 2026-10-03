"""分页的页码导航必须走 `next/link`（客户端跳转），且禁用态不得留空 href。

## 缺陷

`Pagination` 的上一页 / 页码 / 下一页三处都是裸 `<a href>`，每次翻页**整页重载**：
重下 JS、重跑数据请求、首屏再闪一次。仓库内已有正确实现（`CompareBar.tsx` 用
`<Link href={compareHref(ids)}>`），P5 台账把这一项与 `compare-changed` 订阅、
筛选 resync 并列为「同文件内有正确实现可对齐，属低成本高确定性」。

## 一个必须一起处理的细节

首尾两个按钮原先写的是 `href={page > 1 ? … : undefined}`。`next/link` 的 `href`
是**必填** URL，传 `undefined` 过不了 tsc；而「没有 href 的 `<a>`」虽然不可聚焦、
点不动，却仍挂着 `hover:` 样式，看上去像能点。所以禁用态必须换成 `<span>`，
而不是把 `undefined` 换个地方传。

## 跳页表单为什么**不动**

`docs/refactoring-roadmap.md` 把 `<form method="get">` 与裸 `<a>` 并列记为
「每次翻页整页重载」，但两者风险并不对等：原生 GET 表单不依赖 JS 即可工作
（渐进增强），且隐藏域已把除 `page` 外的筛选参数原样带过去；改成 `router.push`
只能省一次整页加载，代价是必须加 `"use client"` 客户端边界、并**亲手重建 query
串**——那才是会真的把筛选丢掉的地方。已在源码里写下这个判断与理由，避免下一个
人照着台账把它「修」坏。
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[2] / "web" / "app" / "components" / "Pagination.tsx"
).read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def test_pagination_imports_next_link():
    assert re.search(r"^import Link from \"next/link\";", _SRC, re.M), "未引入 next/link"


def test_no_bare_anchor_navigation():
    """页码导航不得再出现裸 <a href>（整页重载的根因）。"""
    code = _strip_comments(_SRC)
    bare = re.findall(r"<a\b[^>]*\bhref=", code, re.S)
    assert not bare, f"仍有 {len(bare)} 处裸 <a href>，翻页会整页重载"


def test_disabled_nav_uses_span_not_undefined_href():
    """禁用态必须是 <span>：next/link 的 href 必填，且无 href 的 <a> 观感上仍像可点。"""
    code = _strip_comments(_SRC)
    assert "to={page > 1 ? pageHref(basePath, query, page - 1) : null}" in code, (
        "上一页禁用态应传 null（而非 undefined）"
    )
    assert "to={page < totalPages ? pageHref(basePath, query, page + 1) : null}" in code
    assert 'if (to === null) {' in code and "<span aria-disabled" in code, "禁用态需渲染 span"
    # 只针对 href 收窄：`aria-current={… : undefined}` 是合法的（无当前页时不该输出该属性），
    # 上一版用 ": undefined}" 全局匹配把它也误伤了——断言过宽会把正确代码判成缺陷。
    assert not re.search(r"href=\{[^}]*undefined", code), "不得再向 href 传 undefined"


def test_page_numbers_use_link_and_keep_aria_current():
    code = _strip_comments(_SRC)
    assert re.search(r"<Link\s+key=\{p\}\s+href=\{pageHref\(basePath, query, p\)\}", code), (
        "页码按钮应使用 <Link href=…>"
    )
    assert 'aria-current={p === page ? "page" : undefined}' in code, "当前页的 aria-current 不能丢"


def test_jump_form_stays_native_get_with_reason():
    """跳页表单保留原生 GET（渐进增强 + 不丢筛选），且源码里写明理由。"""
    code = _strip_comments(_SRC)
    assert 'method="get"' in code, "跳页表单应保留原生 GET 提交"
    assert 'action={basePath}' in code
    assert '<input key={k} type="hidden" name={k} value={v} />' in code, (
        "隐藏域负责把筛选参数带过去，删掉就会丢筛选"
    )
    # 理由必须留在源码里，否则下一个人会照台账把它改坏
    assert "use client" in _SRC and "渐进增强" in _SRC, "源码需保留「为何不改」的判断与理由"


def test_query_preservation_in_page_href():
    """pageHref 必须剔掉旧的 page 再写入新的，且 page=1 时省略该参数。"""
    body = re.search(r"function pageHref\((.+?)\n\}", _SRC, re.S)
    assert body, "找不到 pageHref"
    text = body.group(1)
    assert 'if (k !== "page") q.set(k, v);' in text, "必须先剔除旧 page 参数"
    assert 'if (p > 1) q.set("page", String(p));' in text, "page=1 时应省略该参数"
