"""抓取 User-Agent 的**诚实性**（2026-10-02 H6 合规修复）。

README 声明本项目「遵守目标站点 robots.txt 与服务条款」。而
`autohome_sku.fetch_sku_config` 此前用的是一整套 iPhone Safari 指纹
（`Mozilla/5.0 (iPhone…) AppleWebKit… Version/17.0 Mobile/… Safari/604.1`）——
那不是礼貌抓取，是**冒充浏览器**：对方无从识别与联系采集方，
`fetch_robots` / `_record_compliance` 收集的合规结论也就失去意义。

本测试把「不伪装」钉成契约，防止日后有人为了让抓取通过而把伪装加回来。
"""
from __future__ import annotations

import re

from app.sources import autohome_sku, fetcher


def _browser_spoof_tokens(ua: str) -> list[str]:
    """浏览器 UA 才会出现的特征 token。"""
    tokens = ["Mozilla/5.0", "AppleWebKit", "Gecko/", "Chrome/", "Safari/",
              "Version/", "Mobile/", "Trident", "Firefox"]
    return [t for t in tokens if t in ua]


def test_sku_ua_is_not_a_browser_spoof():
    ua = autohome_sku._SKU_UA
    spoofed = _browser_spoof_tokens(ua)
    assert not spoofed, f"SKU 抓取的 UA 仍在伪装浏览器：{spoofed}（{ua}）"


def test_sku_ua_matches_project_crawler_identity():
    """全项目抓取用同一个诚实标识：能一眼看出是采集方、且有联系方式。"""
    assert autohome_sku._SKU_UA == fetcher.DEFAULT_USER_AGENT


def test_crawler_identity_is_identifiable_and_contactable():
    """诚实标识至少要做到：能认出是爬虫 + 有联系渠道。"""
    ua = fetcher.DEFAULT_USER_AGENT
    assert "crawler" in ua.lower(), f"UA 未表明这是采集器：{ua}"
    assert "@" in ua or "contact" in ua.lower(), f"UA 缺少联系渠道：{ua}"


def test_all_fetch_sites_share_one_identity():
    """不得出现第二个 UA 常量——两个身份就意味着有人在单独伪装。"""
    import inspect

    from app.sources import fetcher as f

    src = inspect.getsource(autohome_sku)
    # 允许注释里提到旧常量名，但不允许再有 UA = "Mozilla/..." 之类的赋值
    assert not re.search(r"=\s*\(\s*$[\s\S]{0,200}?Mozilla/5\.0", src), (
        "autohome_sku 里疑似仍有浏览器 UA 字面量"
    )
    assert f.DEFAULT_USER_AGENT
