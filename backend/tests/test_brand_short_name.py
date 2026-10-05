"""品牌简称（去掉「汽车」后缀）可被解析的回归测试。

2026-10-05 端到端实拍：全库 5 个品牌名带「汽车」后缀（零跑汽车 / 小米汽车 /
理想汽车 / 吉利汽车 / 江淮汽车）且 `aliases` **全为空**，而用户口语只说简称。
后果是同一类问题里**一部分品牌能问、一部分不能**：

    「小米有几款车」        -> **全库盘点**（908 个车系），完全不提小米
    「小米的车型有哪些」     -> 「暂时没有可核对的小米在售车型资料」
    「奔驰有几款车」        -> 正确：「奔驰在售车型共 56 款」（品牌名恰好就是简称）
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.catalog.brands import resolve_brand_mentions
from tests.seed import make_brand


def _ids(db: Session, message: str) -> set[int]:
    out = resolve_brand_mentions(db, message)
    return set(out.get("brand_ids") or []) if isinstance(out, dict) else set(out)


def test_generic_suffix_brand_is_reachable_by_short_name(db_session: Session):
    """「小米汽车」应能用「小米」指到——口语里没人说全名。"""
    brand = make_brand(db_session, name="小米汽车")
    for word in ("小米", "小米汽车"):
        assert _ids(db_session, word) == {brand.id}, word
    assert _ids(db_session, "只买小米的") == {brand.id}
    assert _ids(db_session, "我想买小米的车") == {brand.id}


def test_short_name_does_not_double_match_when_brand_exists(db_session: Session):
    """库里另有同名品牌时**不登记简称**；且全名不得同时命中短名品牌。

    实测既有缺陷：`resolve_brand_mentions("零跑汽车")` 曾返回 **[1, 2]**——
    「零跑汽车」消息同时匹配「零跑汽车」与「零跑」两个 brand_id（同一公司的两条
    品牌记录）。长名优先此前只体现在**排序**上，没落到 span 归属。
    """
    full = make_brand(db_session, name="零跑汽车")
    short = make_brand(db_session, name="零跑")
    assert _ids(db_session, "零跑") == {short.id}
    assert _ids(db_session, "零跑汽车") == {full.id}, "全名不得顺带命中短名品牌"
    assert _ids(db_session, "只买零跑汽车") == {full.id}


def test_ambiguous_brands_still_require_intent(db_session: Session):
    """`AMBIGUOUS_BRANDS` 的既有闸门不能被简称登记破坏：理想/长安/大众 需购车意图。"""
    brand = make_brand(db_session, name="理想汽车")
    plain = make_brand(db_session, name="理想")
    assert _ids(db_session, "理想") == set()
    assert _ids(db_session, "理想的预算20万") == set()
    assert _ids(db_session, "我想买理想的车") == {plain.id}
    assert _ids(db_session, "只看理想汽车") == {brand.id}


def test_ambiguous_stem_without_exact_brand_still_blocked(db_session: Session):
    """`AMBIGUOUS_BRANDS` 这道闸**只有在**「有 X汽车、库里没有 X」时才可达。

    上一个用例里 `理想` 恰好是另一个品牌的全名，被「简称若等于别家全名则不登记」
    那道闸挡掉了，这道闸根本没被执行到——**测试全绿但什么都没验到**。
    这里造出可达形态：只有「长安汽车」，没有「长安」。
    """
    changan = make_brand(db_session, name="长安汽车")
    # 裸「长安」是日常词，没有购车意图时**不能**因为存在「长安汽车」就变成品牌
    assert _ids(db_session, "长安") == set()
    assert _ids(db_session, "长安的预算20万") == set()
    # 带购车意图**也不**放行——`AMBIGUOUS_BRANDS` 的既定取舍是「日常词宁可漏也不误判」，
    # 简称登记不改变这条策略；用户必须说全名「长安汽车」。
    assert _ids(db_session, "我想买长安") == set(), "日常词简称不放行，须说全名"
    assert _ids(db_session, "只看长安汽车") == {changan.id}


def test_brand_without_generic_suffix_unaffected(db_session: Session):
    """名字本来就等于简称的品牌（奔驰/特斯拉/比亚迪）行为不得变。"""
    brand = make_brand(db_session, name="奔驰")
    assert _ids(db_session, "只要奔驰") == {brand.id}


def test_resolve_brand_mentions_shape_stable(db_session: Session):
    """`resolve_brand_mentions` 的返回形状不能因这次改动而变化。"""
    make_brand(db_session, name="江淮汽车")
    out = resolve_brand_mentions(db_session, "想买江淮的车")
    assert isinstance(out, dict), out
    assert "brand_ids" in out and "brand_labels" in out, out
    assert out["brand_ids"], out


def test_strip_generic_suffix():
    from app.catalog.brands import _strip_generic_suffix

    assert _strip_generic_suffix("小米汽车") == "小米"
    assert _strip_generic_suffix("吉利汽车") == "吉利"
    # 剥完太短或剥不动时返回空串
    assert _strip_generic_suffix("奔驰") == ""
    assert _strip_generic_suffix("车") == ""
