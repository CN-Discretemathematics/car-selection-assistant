"""`display_name` 不重复品牌词的测试。

原规则是 `not series.name.startswith(brand.name)`——只挡**完全**前缀，于是
品牌「小米汽车」+ 车系「小米SU7」拼出「**小米汽车小米SU7**」。

更要命的是**重复词未必在开头**：真实库 24 个车系是把品牌词放在中间
（「启源长安启源A06」「大众一汽-大众CC」「本田东风本田S7」…）。所以判据必须是
「品牌核心词**已在车系名里**」而不是「在车系名开头」——这一点是反向验证的
变异体逼出来的，最初只按前缀判，漏掉了这 24 个。

同时钉住**不能**被误伤的正确拼接：「特斯拉」+「Model Y」仍应是「特斯拉Model Y」。
"""
from __future__ import annotations

from app.catalog.series_index import _brand_leads_series, display_name


class _S:
    def __init__(self, name: str) -> None:
        self.name = name


class _B:
    def __init__(self, name: str | None) -> None:
        self.name = name


def _dn(series: str, brand: str | None) -> str:
    return display_name(_S(series), _B(brand))  # type: ignore[arg-type]


# ── 修掉的：品牌词重复（开头）─────────────────────────────────────────────
def test_brand_generic_suffix_not_repeated():
    """「小米汽车」+「小米SU7」→ 「小米SU7」，不是「小米汽车小米SU7」。"""
    assert _dn("小米SU7", "小米汽车") == "小米SU7"


def test_brand_generic_suffix_not_repeated_more_series():
    """其余 8 个真实中招车系，同一个成因。"""
    for series, brand in [
        ("小米SU7 Ultra", "小米汽车"),
        ("小米YU7", "小米汽车"),
        ("吉利ICON", "吉利汽车"),
        ("吉利牛仔", "吉利汽车"),
        ("江淮A5 PLUS", "江淮汽车"),
        ("江淮QX PHEV", "江淮汽车"),
        ("江淮X8 E家", "江淮汽车"),
        ("江淮X8 PLUS", "江淮汽车"),
    ]:
        assert _dn(series, brand) == series, f"{brand}+{series} 仍在重复品牌词"


def test_exact_prefix_still_kept_before_generic_suffix():
    """完全前缀的老行为不能退化：品牌「理想」+ 车系「理想i6」→ 「理想i6」。"""
    assert _dn("理想i6", "理想") == "理想i6"


# ── 修掉的：品牌词重复（中间）——第一版按前缀判时整组漏掉 ──────────────────
def test_brand_word_in_the_middle_is_not_repeated():
    """品牌词在车系名**中间**时同样不能重复——判据必须是包含而非前缀。"""
    for series, brand in [
        ("长安启源A06", "启源"),
        ("长安启源A07", "启源"),
        ("长安启源E07", "启源"),
        ("长安启源Q05", "启源"),
        ("长安启源Q05经典", "启源"),
        ("长安启源Q07", "启源"),
        ("吉利几何A", "几何"),
        ("吉利几何E萤火虫", "几何"),
        ("吉利几何G6", "几何"),
        ("吉利几何M6", "几何"),
        ("一汽-大众CC", "大众"),
        ("东风本田S7", "本田"),
        ("广汽本田P7", "本田"),
        ("北京现代ix35", "现代"),
        ("东风风神E70", "风神"),
        ("上汽大通MAXUS H90房车", "大通"),
    ]:
        assert _dn(series, brand) == series, f"{brand}+{series} 仍在重复品牌词"


# ── 不能被误伤的：品牌与车系名确实不同 ────────────────────────────────────
def test_unrelated_brand_still_prefixed():
    for series, brand, want in [
        ("Model Y", "特斯拉", "特斯拉Model Y"),
        ("汉", "比亚迪", "比亚迪汉"),
        ("星愿", "银河", "银河星愿"),
        ("帕萨特", "大众", "大众帕萨特"),
        ("汉兰达", "丰田", "丰田汉兰达"),
        ("i6", "理想", "理想i6"),
    ]:
        assert _dn(series, brand) == want, f"{brand}+{series} 被误改"


def test_latin_brand_token_does_not_suppress_prefix():
    """品牌含纯英文词时不能靠它判定——「特斯拉」的判定词是中文「特斯拉」，
    「Model 3」里没有它，仍要拼；但「AITO 问界」里的「问界」是有效判据。"""
    assert _dn("Model 3", "特斯拉") == "特斯拉Model 3"
    assert _brand_leads_series("AITO 问界", "问界M8") is True
    assert _dn("问界M8", "AITO 问界") == "问界M8"


def test_missing_brand_name_is_tolerated():
    """品牌名为空/None 时只返回车系名（原行为，不得抛）。"""
    assert _dn("星愿", None) == "星愿"
    assert _dn("星愿", "") == "星愿"

