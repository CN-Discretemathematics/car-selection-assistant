"""`maintenance` 维度的「品牌规模」必须是**全库口径**，与筛选条件无关。

## 缺陷（2026-10-03 修）

`recommendation_tool` 里 `brand_series_count` 原先在**过滤后的候选集**上累加：

    for s in series_map.values():          # series_map 只含通过硬约束的车系
        brand_series_count[s.brand_id] += 1

而它是喂给 `maintenance` 维的「品牌规模 / 维修网络」代理（`min(1/5, 1) * 0.3`）。
**品牌规模是品牌的固有属性，不该随用户筛选条件变化**：

  - 筛选得越窄 → 留下的车系越少 → 「规模」越小 → 维护便利性评分被系统性压低；
  - 一个只卖 1 个车系的品牌和卖 20 个车系的品牌，只要都只剩 1 个车系通过筛选，
    就会得到**完全相同**的「规模」分——代理彻底失效。

## 为什么用「score 不变」来测，而不是直接读维度分

`recommendation_tool` 在返回前把内部 `_dims` 弹掉了（`item.pop("_dims")`），
维度分对外不可见。所以这里测一个**等价且可观测**的不变式：

    同一款型 A 的得分，不该因为「A 的兄弟车型被筛选条件挡掉了」而变化。

改动前必然不成立（A 的兄弟一被挡，A 的品牌规模就变小、分数就掉）；改动后成立。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.schemas import Budget, UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

TARGET = "大品牌车系0"   # 唯一用 mpv 车身，用来把筛选收窄到只剩它


def _seed(db: Session) -> None:
    source = make_source(db, name="汽车之家")
    big = make_brand(db, name="大品牌", source=source)      # 6 个车系
    small = make_brand(db, name="小品牌", source=source)    # 1 个车系
    for i in range(6):
        body = "mpv" if i == 0 else "sedan"
        s = make_series(db, big, name=f"大品牌车系{i}", body_type=body,
                        energy_types=("BEV",), source=source)
        y = make_year(db, s)
        make_variant(db, s, y, price_cny="150000", source=source)
    s = make_series(db, small, name="小品牌车系", body_type="sedan",
                    energy_types=("BEV",), source=source)
    y = make_year(db, s)
    make_variant(db, s, y, price_cny="150000", source=source)
    db.commit()


def _score_of(db: Session, name: str, profile: UserProfile) -> float:
    res = recommendation_tool(db, profile, limit=20)
    assert res["count"] > 0, "对照组必须有候选"
    for v in res["variants"]:
        if v.get("series_name") == name:
            return v["score"]
    raise AssertionError(f"结果里找不到 {name}（返回了 {[v.get('series_name') for v in res['variants']]}）")


def _budget() -> Budget:
    return Budget(min=100000, max=200000)


def test_score_does_not_drop_when_sibling_series_are_filtered_out(db_session: Session):
    """核心不变式：兄弟车型被筛掉，不该让目标车型掉分。

    改动前：大品牌有 6 个车系时规模分 1.0；一旦只留下 1 个，规模分掉到 0.2，
    `maintenance` 从 1.0 降到 0.46，`score` 随之下降——于是「你筛得越准，
    它看起来越难养」。
    """
    _seed(db_session)

    loose = _score_of(db_session, TARGET, UserProfile(budget=_budget()))

    narrow = UserProfile(budget=_budget())
    narrow.body_type = ["mpv"]          # 只留下目标车型
    tight = _score_of(db_session, TARGET, narrow)

    assert tight == loose, (
        f"同一款型的得分随筛选条件变化：宽筛选 {loose} → 窄筛选 {tight}。"
        "说明品牌规模仍在用过滤后候选集计算。"
    )


def test_scale_proxy_still_separates_big_and_small_brand(db_session: Session):
    """改坏了要能看出来：规模代理仍必须区分大小品牌。"""
    _seed(db_session)
    res = recommendation_tool(db_session, UserProfile(budget=_budget()), limit=20)
    scores = {v.get("series_name"): v["score"] for v in res["variants"]}
    big = [s for n, s in scores.items() if n and n.startswith("大品牌")]
    small = [s for n, s in scores.items() if n and n.startswith("小品牌")]
    assert big and small, f"两类品牌都应出现在结果里：{list(scores)}"
    assert min(big) > max(small), (
        f"大品牌任意款型都应高于小品牌：big_min={min(big)} small={max(small)}"
    )
