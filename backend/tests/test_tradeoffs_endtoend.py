"""取舍叙事的端到端行为（2026-10-02 修活 L3）。

前面的 `test_tradeoffs*` 测的是判定规则本身；这里测**真实推荐链路**的输出，
确保：取舍项真的被填进 `variants[*].tradeoffs`、且缺失数据不冒充劣势。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.schemas import UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_sales, make_source, make_series, make_variant, make_year


def _seed(db: Session, specs: list[dict]) -> None:
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)
    for i, spec in enumerate(specs):
        series = make_series(
            db, brand, name=f"车系{i}", body_type=spec.get("body_type", "sedan"),
            energy_types=(spec.get("energy_type", "BEV"),), source=source,
        )
        year = make_year(db, series)
        make_variant(
            db, series, year, config_version="标准版",
            energy_type=spec.get("energy_type", "BEV"),
            price_cny=spec.get("price", "150000"),
            facts=spec.get("facts"), source=source,
        )
        if spec.get("sales"):
            make_sales(db, series, "2026-08", 10000, source=source)
    db.commit()


def test_tradeoffs_are_populated_end_to_end(db_session: Session):
    """两款车：长的空间好、短的价格低，取舍项必须真的出现在输出里。"""
    _seed(db_session, [
        {"price": "200000", "facts": [("尺寸", "length_mm", "4900", "mm", None)]},
        {"price": "120000", "facts": [("尺寸", "length_mm", "4500", "mm", None)]},
    ])
    out = recommendation_tool(db_session, UserProfile(budget={"min": 100000, "max": 250000}), limit=10)
    variants = out["variants"]
    assert len(variants) == 2
    assert any(v["tradeoffs"] for v in variants), "取舍项仍为空：取舍叙事没接上"
    # 不应出现内部措辞
    for v in variants:
        for t in v["tradeoffs"]:
            assert not any(x in t for x in ("未参与", "暂无", "数据源", "未披露")), t


def test_missing_data_does_not_become_a_tradeoff(db_session: Session):
    """关键反编造断言：只有长的一台有车长数据，短的那台**没有**。

    此时「空间」这一维对短车是**库内缺数据**，绝不能说它「空间不及最优候选」——
    那等于用缺失数据编造一个负面事实（违反设计原则第 1 条）。
    """
    _seed(db_session, [
        {"price": "200000", "facts": [("尺寸", "length_mm", "4900", "mm", None)]},
        {"price": "120000", "facts": []},          # 无任何尺寸事实
    ])
    out = recommendation_tool(db_session, UserProfile(budget={"min": 100000, "max": 250000}), limit=10)
    for v in out["variants"]:
        assert not any("空间" in t for t in v["tradeoffs"]), (
            f"缺车长数据却被说成空间劣势：{v['tradeoffs']}"
        )


def test_identical_candidates_get_no_tradeoffs(db_session: Session):
    """数据完全相同的两款不该被无差别贴上「不及最优」标签。"""
    facts = [("尺寸", "length_mm", "4700", "mm", None)]
    _seed(db_session, [
        {"price": "150000", "facts": facts},
        {"price": "150000", "facts": list(facts)},
    ])
    out = recommendation_tool(db_session, UserProfile(budget={"min": 100000, "max": 200000}), limit=10)
    for v in out["variants"]:
        assert v["tradeoffs"] == [], f"完全相同却报了取舍：{v['tradeoffs']}"


def test_private_keys_are_stripped(db_session: Session):
    """第二遍用来算取舍的私有键不得泄漏到对外返回结构。"""
    _seed(db_session, [
        {"price": "200000", "facts": [("尺寸", "length_mm", "4900", "mm", None)]},
        {"price": "120000", "facts": [("尺寸", "length_mm", "4500", "mm", None)]},
    ])
    out = recommendation_tool(db_session, UserProfile(), limit=10)
    for v in out["variants"]:
        assert "_dims" not in v and "_measured" not in v, "私有计算字段泄漏到返回结构"
