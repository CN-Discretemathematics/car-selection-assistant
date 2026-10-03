"""L2 端到端：usage 权重入口是否**真的改变排序**（2026-10-03 补齐后新增）。

只测 extract_hints 的 dict 变化是不够的——那证明不了「用户最看重的用途真的影响了
推荐结果」。本文件把 sales-agent-proposal §2 的销售话术落成可复现的排序断言。

## 对抗样本怎么设计的（第一版失败了，值得记下来）

我最初的设计是「轿车预算正中 vs SUV 超预算」，结果 SUV **根本没进结果**——预算是
**硬约束**（SQL 下推 price BETWEEN），超预算的车在评分之前就被挡掉了，拿它当对手
等于什么都没测。凡是拿硬约束去和软权重对抗的设计，都要先确认对手还活着。

改成：两车都**在预算区间内**，但 budget 维度按「距区间中点的距离」打分，于是
  轿车 A：17.5 万 = 区间中点 → budget 1.0；车长 5000 / 功率 250（都更强）
  SUV  B：20 万 = 区间上沿 → budget 0.5；车长 4600 / 功率 150（都更弱）
唯一让 B 占优的是**用途**（家庭 → SUV/MPV 匹配，轿车得 0）。

手算分差（score_A - score_B）：
  默认 usage 0.15：.30×0.5 - .15×1.0 + .10×0.444 + .10×0.455 = +0.090 → 轿车胜
  强调后 usage 0.35（Σ 1.00→1.20）：.15 - .35 + .044 + .046 = -0.110 → SUV 胜
两边都有约 0.1 的余量，不是压线过的设计。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.engine import extract_hints, merge_profile
from app.agent.schemas import UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

BUDGET_LO, BUDGET_HI = 150000, 200000


def _seed_adversarial(db: Session) -> tuple[int, int]:
    """轿车贵在正中+尺寸动力（budget/space/power 占优），SUV 贵在家庭用途匹配。"""
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)
    sedan = make_series(db, brand, name="长轴轿车", body_type="sedan",
                        energy_types=("BEV",), source=source)
    suv = make_series(db, brand, name="家用SUV", body_type="suv",
                      energy_types=("BEV",), source=source)
    y1 = make_year(db, sedan)
    a = make_variant(db, sedan, y1, price_cny="175000",
                     facts=[("尺寸", "length_mm", "5000", "mm", None),
                            ("动力", "最大功率(kW)", "250", "kW", None)], source=source)
    y2 = make_year(db, suv)
    b = make_variant(db, suv, y2, price_cny="200000",
                     facts=[("尺寸", "length_mm", "4600", "mm", None),
                            ("动力", "最大功率(kW)", "150", "kW", None)], source=source)
    db.commit()
    return a.id, b.id


def _profile(msg: str) -> UserProfile:
    return merge_profile(UserProfile(), extract_hints(msg))


def test_both_adversaries_survive_hard_constraints(db_session: Session):
    """先确认对手还活着：预算是硬约束，超预算的车在评分前就被挡掉。"""
    sedan_id, suv_id = _seed_adversarial(db_session)
    res = recommendation_tool(db_session, _profile("15万到20万，家庭用车"), limit=5)
    ids = {v["variant_id"] for v in res["variants"]}
    assert {sedan_id, suv_id} <= ids, (
        "两台车都必须通过硬约束进入评分——否则下面的排序断言在测一个不存在的对手"
    )


def test_usage_emphasis_flips_recommendation(db_session: Session):
    """「最看重家用」把 usage 权重从 0.15 抬到 0.35，足以翻掉 budget/space/power 的优势。"""
    sedan_id, suv_id = _seed_adversarial(db_session)

    plain = _profile("15万到20万，家庭用车")
    emph = _profile("15万到20万，家庭用车，最看重家用")

    assert plain.usage == ["家庭"], "两种说法都应识别出家庭用途（差异只在强调词）"
    assert not plain.weights, "无强调词不加权"
    assert emph.weights == {"usage": 0.35}, "「最看重家用」应产出 usage 绝对权重（默认 0.15 + 0.2）"

    r_plain = recommendation_tool(db_session, plain, limit=5)
    r_emph = recommendation_tool(db_session, emph, limit=5)
    assert r_plain["weights_used"]["usage"] == 0.15
    assert r_emph["weights_used"]["usage"] == 0.35

    by_plain = {v["variant_id"]: v["score"] for v in r_plain["variants"]}
    by_emph = {v["variant_id"]: v["score"] for v in r_emph["variants"]}
    assert {sedan_id, suv_id} <= set(by_plain) and {sedan_id, suv_id} <= set(by_emph)

    best_plain = max(by_plain, key=lambda i: by_plain[i])
    best_emph = max(by_emph, key=lambda i: by_emph[i])
    assert best_plain == sedan_id, (
        f"默认权重下应是 budget/space/power 占优的轿车胜出，实得 {best_plain}"
        "（若此项失败，对抗设计已失效，后续断言失去意义）"
    )
    assert best_emph == suv_id, (
        f"抬升 usage 权重后应是家庭用途匹配的 SUV 胜出，实得 {best_emph}——"
        "若失败，说明 L2 补齐的 usage 权重入口对排序毫无影响（装饰品）"
    )


def test_backseat_emphasis_maps_to_space():
    """「最看重后排」落 space 维（space 由 length_mm 车长归一）。"""
    assert extract_hints("最看重后排")["weights"] == {"space": 0.3}
    assert extract_hints("我比较看重后排空间")["weights"] == {"space": 0.3}
    # 仍然要求强调词：顺口一提不该被当成硬偏好
    assert "weights" not in extract_hints("后排要宽敞一点")
