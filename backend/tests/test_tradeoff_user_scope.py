"""取舍叙事只谈**用户明确提过**的维度（2026-10-05 用户实测反馈）。

## 生产缺陷复现

用户输入「我最看重动力，有哪些车推荐」→ 选预算 → 选用途 → 拿到推荐，
推荐文案里出现：

> 注意妥协项：空间不及本批最优候选

**空间用户一个字都没提过。** 根因在 `recommendation_tool` 算取舍时的对照集：

```python
weights = {**DEFAULT_WEIGHTS, **profile.weights}   # 含全部 8 个默认维度
for d in item["_measured"]:
    if d in weights:                              # ← 于是必然是全部 8 维
        best_by_dim[d] = max(...)
```

那不是编造（空间数据真、差距真），但是**不相关的噪音**——把用户没提的维度
摆出来，等于替用户决定他该在意什么。

## 本组测试守两件事

1. **不提的维度不报**（噪声消失）；
2. **提了的维度仍然照报**（不能把取舍整体删掉——那是另一个极端，同样是错）。
"""
from __future__ import annotations

from app.agent import tools as T


# ── 机制层：_tradeoff_gaps 只认传入的 best_by_dim ────────────────────────────
def test_tradeoff_gaps_reports_raised_dimension():
    """用户提了空间 → 空间真有差距时**必须**报出来。"""
    dims = {"space": 0.4, "power": 0.9}
    measured = {"space", "power"}
    best = {"space": 1.0, "power": 1.0}

    gaps = T._tradeoff_gaps(dims, measured, best)

    assert any("空间" in g for g in gaps), gaps
    assert not any("动力" in g for g in gaps), gaps


def test_tradeoff_gaps_ignores_dimensions_absent_from_best():
    """best_by_dim 里没有的维度（= 用户没提的）一律不报。"""
    dims = {"space": 0.2, "power": 0.9}
    measured = {"space", "power"}

    gaps = T._tradeoff_gaps(dims, measured, {"power": 1.0})   # space 未进对照集

    assert gaps == [], gaps


def test_tradeoff_gaps_respects_gap_threshold():
    """差距小于 _TRADEOFF_GAP 是噪声，即便维度被提了也不报。"""
    dims = {"power": 0.95}
    measured = {"power"}

    assert T._tradeoff_gaps(dims, measured, {"power": 1.0}) == []


def test_tradeoff_gaps_skips_unmeasured_dimension():
    """库内无该项数据时不得说它「不及最优」——那是用缺失数据编造负面事实。"""
    dims = {"power": 0.1}
    measured: set[str] = set()          # 库内没有动力数据

    assert T._tradeoff_gaps(dims, measured, {"power": 1.0}) == []


# ── 集成层：recommendation_tool 的对照集构造 ─────────────────────────────────
def _profile_with_weights(db_session, weights: dict[str, float]):
    from app.agent.schemas import Budget, UserProfile

    profile = UserProfile()
    profile.budget = Budget(min=100000, max=200000)
    profile.usage = ["家庭出行"]
    profile.passengers = 5
    profile.weights = dict(weights)
    return profile


def _seed_two_cars(db, source, big, small):
    """造两台车：大车空间明显更好，小车空间差但动力更好。"""
    from tests.seed import make_variant, make_year

    y1, y2 = make_year(db, big), make_year(db, small)
    make_variant(
        db, big, y1, config_version="大空间版", energy_type="BEV",
        price_cny="180000", source=source,
        facts=[("尺寸", "length_mm", "5200", "mm", None),
               ("空间", "rear_seat_mm", "1000", "mm", None),
               ("动力", "power_kw", "200", "kW", None)],
    )
    make_variant(
        db, small, y2, config_version="小空间版", energy_type="BEV",
        price_cny="150000", source=source,
        facts=[("尺寸", "length_mm", "4300", "mm", None),
               ("空间", "rear_seat_mm", "880", "mm", None),
               ("动力", "power_kw", "320", "kW", None)],
    )


def test_user_raised_dimension_gap_is_still_reported(db_session):
    """只提「空间」时，小空间车的空间短板**必须**出现在取舍里。"""
    from tests.seed import make_brand, make_series, make_source

    source = make_source(db_session, name="官方来源一")
    brand = make_brand(db_session, name="测试品牌", source=source)
    big = make_series(db_session, brand, name="空间大王", body_type="suv",
                      energy_types=("BEV",), source=source)
    small = make_series(db_session, brand, name="小车", body_type="hatchback",
                        energy_types=("BEV",), source=source)
    _seed_two_cars(db_session, source, big, small)
    db_session.flush()

    profile = _profile_with_weights(db_session, {"space": 0.5})
    result = T.recommendation_tool(db_session, profile, limit=5)

    tradeoffs = [t for v in result["variants"] for t in (v.get("tradeoffs") or [])]
    assert any("空间" in t for t in tradeoffs), (
        f"用户提了空间、空间真有差距，取舍里却没有：{tradeoffs}"
    )


def test_unraised_dimension_gap_is_not_reported(db_session):
    """只提「动力」时，空间差距**不得**出现在取舍里（这就是本次修的缺陷）。"""
    from tests.seed import make_brand, make_series, make_source

    source = make_source(db_session, name="官方来源二")
    brand = make_brand(db_session, name="测试品牌B", source=source)
    big = make_series(db_session, brand, name="空间大王B", body_type="suv",
                      energy_types=("BEV",), source=source)
    small = make_series(db_session, brand, name="小车B", body_type="hatchback",
                        energy_types=("BEV",), source=source)
    _seed_two_cars(db_session, source, big, small)
    db_session.flush()

    profile = _profile_with_weights(db_session, {"power": 0.5})
    result = T.recommendation_tool(db_session, profile, limit=5)

    tradeoffs = [t for v in result["variants"] for t in (v.get("tradeoffs") or [])]
    assert not any("空间" in t for t in tradeoffs), (
        f"用户没提空间，取舍里却出现空间：{tradeoffs}"
    )


def test_no_weights_no_tradeoffs_at_all(db_session):
    """一个维度都没提 → 整段取舍不出现（而不是列举全部 8 维的差距）。"""
    from tests.seed import make_brand, make_series, make_source

    source = make_source(db_session, name="官方来源三")
    brand = make_brand(db_session, name="测试品牌C", source=source)
    big = make_series(db_session, brand, name="空间大王C", body_type="suv",
                      energy_types=("BEV",), source=source)
    small = make_series(db_session, brand, name="小车C", body_type="hatchback",
                        energy_types=("BEV",), source=source)
    _seed_two_cars(db_session, source, big, small)
    db_session.flush()

    profile = _profile_with_weights(db_session, {})
    result = T.recommendation_tool(db_session, profile, limit=5)

    tradeoffs = [t for v in result["variants"] for t in (v.get("tradeoffs") or [])]
    assert tradeoffs == [], f"用户没提任何维度，却报出了取舍：{tradeoffs}"
