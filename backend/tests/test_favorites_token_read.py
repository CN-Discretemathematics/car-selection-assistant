"""收藏页不得在 **render 期**读 localStorage，且读取必须有容错（2026-10-03 修）。

## 两个缺陷（台账 `web/lib/auth.ts:5-13` / `favorites/page.tsx:13` 那一条）

1. **`getToken()` 只有 SSR 守卫、没有 try/catch**。Safari 无痕模式 / 禁用 Cookie /
   存储配额异常时 `localStorage.getItem` 会**抛异常**；而收藏页在 render 期调它，
   一抛就**整页渲染失败**。写法对齐 `CompareBar.readCompareIds`（同仓早就有）。

2. **`favorites/page.tsx:13` 用 lazy `useState(() => getToken())`**。`useState`
   的初始化器在**首次渲染**执行——服务端返回 null（SSR 守卫），客户端可能拿到真
   token，两边渲染出**不同的树**，React 报 hydration 不一致。更要紧的是这个值
   **只算一次**：之后 token 怎么变（另一标签页登录/退出）本页都不知道。

## 为什么不能只是「挪进 useEffect」

挪完之后，未读到 token 的那一帧会落进 `!token` 分支，页面对**已登录用户**显示
「登录后才能查看收藏」——先是句假话，再变回来。故另加 `tokenReady` 区分
「还没读」与「读了、确实没登录」，前者展示骨架屏。

与本会话修过的 `HomeFilters`（URL 不 resync）、`AddToCompareButton`（自建第二份
状态）同族：**把本该从真值源派生的值在 render 期定死**。
"""
from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"
_AUTH = (_WEB / "lib" / "auth.ts").read_text(encoding="utf-8")
_FAV = (_WEB / "app" / "favorites" / "page.tsx").read_text(encoding="utf-8")


def _strip(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def test_get_token_keeps_ssr_guard_and_adds_try_catch():
    fn = re.search(r"export function getToken\(\)(.+?)\n\}", _AUTH, re.S)
    assert fn, "找不到 getToken"
    body = fn.group(1)
    assert 'if (typeof window === "undefined") return null;' in body, "SSR 守卫不能丢"
    assert "try {" in body and "catch" in body, (
        "读取必须有容错——无痕模式 getItem 会抛，而调用方在 render 期调用它"
    )
    assert re.search(r"catch \{\s*return null;", body), "读失败应按「未登录」处理"


def test_favorites_does_not_read_token_during_render():
    fav = _strip(_FAV)
    assert "useState<string | null>(() => getToken())" not in fav, (
        "token 不得在 render 期读取（lazy useState 初始化器也算 render 期）"
    )
    assert not re.search(r"const \[[^\]]*\]\s*=\s*useState\([^)]*getToken\(\)", fav), (
        "任何 useState 初值里都不得出现 getToken()"
    )
    assert re.search(r"useEffect\(\(\) => \{\s*setToken\(getToken\(\)\);", fav), (
        "token 应在 effect 里读取"
    )


def test_favors_distinguishes_not_read_yet_from_not_logged_in():
    """未读到 token 不能直接显示「登录后才能查看收藏」——那对已登录用户是句假话。"""
    fav = _strip(_FAV)
    assert "tokenReady" in fav, "缺少 tokenReady：无法区分「还没读」与「确实没登录」"
    assert re.search(r"!tokenReady \?", fav), "渲染分支需先判 tokenReady"
    order = fav.index("!tokenReady ?") if "!tokenReady ?" in fav else -1
    assert order >= 0 and order < fav.index("!token ?"), (
        "tokenReady 的判断必须排在 !token 之前"
    )
    assert re.search(r"setTokenReady\(true\);", fav), "读完后必须置 ready"


def test_token_change_triggers_reload():
    """token 必须在 effect 依赖里——否则登录状态变了本页也不重新拉收藏。"""
    fav = _strip(_FAV)
    effect = re.search(r"useEffect\(\(\) => \{\s*if \(!token\) return;(.+?)\}, \[token\]\);", fav, re.S)
    assert effect, "拉取收藏的 effect 依赖必须是 [token]"


def test_skeleton_reused_instead_of_duplicated():
    fav = _strip(_FAV)
    assert fav.count('aria-label="加载中"') == 1, (
        "骨架屏应提取为变量复用；两处内联同样的 4 个占位块是复制"
    )
