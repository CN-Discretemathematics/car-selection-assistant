"""取舍叙事（提案 §5 硬判据 2）的**正确渲染率**——CI 常驻。

## 为什么「不报错」不等于「没编造」

取舍叙事全是**负面断言**（「这台空间不如别的车」）。负面断言是幻觉最容易藏身的
地方，而且它**不会抛异常**——编出来的话读起来完全通顺。所以必须有指标，
且指标必须**证明自己抓得住**。

## 这组测试最容易被自己骗的地方

第一版种子**什么都没测到**（实测：拆掉「有库内实值」与「达阈值」两道防线，
判据仍报 100%）。两个原因：

1. `comfort`/`intelligence` 只在事实的**类别**命中
   `_COMFORT_CATEGORIES`/`_INTELLIGENCE_CATEGORIES` 时才算 measured。
   当时随手写的类别「配置信息」不被识别 → 这两维**全批都没测到**；
2. 没有**亚阈值**的差距 → `_TRADEOFF_GAP` 那道闸门形同虚设。

所以这里除了「不该报的没报」，**必须同时断言「该报的报了」**——
只测前者的话，一个什么都不报的评分器能拿满分。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.schemas import Budget, UserProfile
from app.agent.tools import _TRADEOFF_GAP, recommendation_tool
from tests.seed import make_brand, make_series, make_source, make_variant, make_year
from tools.eval_tradeoffs import judge

COMFORT_FACT = ("舒适性", "座椅通风", "有", None, None)
INTEL_FACT = ("智能座舱", "自动泊车", "有", None, None)


def _seed(db: Session) -> dict[str, int]:
    """按缺陷反着造：缺数据的车、亚阈值差的车、真正大幅落后的车各一台。"""
    source = make_source(db, name="评测来源")
    brand = make_brand(db, name="评测品牌", source=source)
    plan = [
        ("基线车", 4800, 250, True, True),
        ("微短车", 4750, 250, True, True),     # 比基线**短** 50mm → 空间差 0.056 < 阈值
        ("缺车长车", None, 250, True, True),   # 空间**无库内数据**
        ("缺舒适车", 4800, 250, False, True),  # 舒适**无库内数据**
        ("弱动车", 4800, 100, True, True),     # 动力大幅落后 → **应当**报出
    ]
    out: dict[str, int] = {}
    for name, length, power, has_comfort, has_intel in plan:
        s = make_series(db, brand, name=name, body_type="suv", energy_types=("BEV",), source=source)
        y = make_year(db, s)
        facts: list[tuple] = []
        if length is not None:
            facts.append(("参数信息", "长度(mm)", str(length), "mm", None))
        if power is not None:
            facts.append(("参数信息", "最大功率(kW)", str(power), "kW", None))
        if has_comfort:
            facts.append(COMFORT_FACT)
        if has_intel:
            facts.append(INTEL_FACT)
        out[name] = make_variant(db, s, y, price_cny="150000", facts=facts, source=source).id
    db.commit()
    return out


@pytest.fixture()
def tradeoffs(db_session: Session):
    ids = _seed(db_session)
    profile = UserProfile(budget=Budget(min=100000, max=200000))
    res = recommendation_tool(db_session, profile, limit=10, include_dims=True)
    by_name = {v["series_name"]: v for v in res["variants"]}
    return by_name, profile, res, ids


def test_include_dims_does_not_leak_into_default_path(db_session: Session):
    """`include_dims` 只是评测开关：默认调用不得带出 `_dims`/`_measured`。"""
    _seed(db_session)
    profile = UserProfile(budget=Budget(min=100000, max=200000))
    default = recommendation_tool(db_session, profile, limit=10)
    for v in default["variants"]:
        assert "_dims" not in v and "_measured" not in v, "私有键泄漏进生产响应体"

    with_dims = recommendation_tool(db_session, profile, limit=10, include_dims=True)
    assert any("_dims" in v for v in with_dims["variants"]), "评测模式没拿到逐维数据，判据无从复核"


def test_missing_data_is_never_reported_as_a_tradeoff(tradeoffs):
    """库内**无数据**的维度不得被说成「不及最优」——那是用缺失数据编造负面事实。"""
    by_name, *_ = tradeoffs
    assert by_name["缺车长车"]["tradeoffs"] == [], (
        f"缺车长车没有车长数据，却被说成取舍：{by_name['缺车长车']['tradeoffs']}"
    )
    assert by_name["缺舒适车"]["tradeoffs"] == [], (
        f"缺舒适车没有舒适配置数据，却被说成取舍：{by_name['缺舒适车']['tradeoffs']}"
    )


def test_sub_threshold_gap_is_not_a_tradeoff(tradeoffs):
    """落后不足 `_TRADEOFF_GAP` 是噪声，报出来只会稀释真正的取舍。

    ⚠️ 这台车必须**比基线更短**。第一版写成「略长车」（4850 > 4800），
    它在空间上得分**更高**（0.611 > 0.556），`base - slight` 是负数——
    断言 `0 < base - slight` 当场红了。种子造错方向，测的就不是闸门了。
    """
    by_name, *_ = tradeoffs
    base = by_name["基线车"]["_dims"]["space"]
    shorter = by_name["微短车"]["_dims"]["space"]
    assert 0 < base - shorter < _TRADEOFF_GAP, "种子没造出亚阈值差距，测不到闸门"
    assert by_name["微短车"]["tradeoffs"] == [], (
        f"亚阈值差距被当成了取舍：{by_name['微短车']['tradeoffs']}"
    )


def test_real_shortfall_is_still_reported(tradeoffs):
    """**反向断言**：真正大幅落后的那台**必须**报出取舍。

    没有这条，一个「什么都不报」的评分器能拿满分——这组测试就白写了。
    """
    by_name, *_ = tradeoffs
    claims = by_name["弱动车"]["tradeoffs"]
    assert any("动力" in c for c in claims), f"真正的动力劣势没被报出来：{claims}"


def test_baseline_reports_no_tradeoff(tradeoffs):
    """各维度都不差的那台应报「无明显妥协」——不是漏报，是真的没有。"""
    by_name, *_ = tradeoffs
    assert by_name["基线车"]["tradeoffs"] == []


def test_judge_reports_full_rate_on_this_fixture(tradeoffs):
    """判据本身在这组对抗数据上必须是 100%，且一条编造都没有。"""
    by_name, profile, res, _ = tradeoffs
    by_name_ = {v["series_name"]: v for v in res["variants"]}
    rep = judge({**res, "variants": list(by_name_.values())}, profile)
    assert rep["failures"] == [], f"判据抓到编造：{rep['failures']}"
    assert rep["rate"] == 1.0
    assert rep["claims"] >= 1, "一条取舍都没产出，判据是空转"
