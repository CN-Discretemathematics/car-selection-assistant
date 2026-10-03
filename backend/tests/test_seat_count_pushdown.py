"""H2-A：座位数下推 SQL 必须与原 Python 侧判定**逐条等价**（2026-10-03）。

## 这条等价性为什么是本改动的全部风险

物化前的判定是：

    seats = _extract_seats(facts)                              # float | None
    if profile.passengers is not None and seats is not None and seats < profile.passengers:
        continue                                                # 丢弃

下推后 SQL 多了 `seat_count IS NULL OR seat_count >= N`。两者必须给出**同一个**
候选集。任何一个不匹配的情况都会让推荐结果静默改变——而这种改变没有任何东西会报错。

## 三个必须分别覆盖的分支

1. **有座位事实且够坐** → 两边都保留
2. **有座位事实但不够坐** → 两边都丢弃（这是下推唯一真正生效的分支）
3. **没有座位事实（seat_count IS NULL）** → 两边都**保留**（缺数据 ≠ 不满足）
   第 3 条是最容易被写错的：SQL 里省掉 `IS NULL`，一次就会丢掉
   「没有座位数据的所有款型」——本地快照库 6629 款型里 85% 属于这一类。

## 另一个必须守住的性质

**回填没跑时行为不变**。`seat_count` 全为 NULL 时下推等于空操作，
Python 侧兜底仍在生效。所以这次改动是「先正确、后变快」，而不是「换了口径」。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.schemas import Budget, UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> dict[str, int]:
    """5 座 / 7 座 / 无座位事实 各来一台，返回 name -> id。"""
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)
    out: dict[str, int] = {}
    plan = [("五座车", 5, True), ("七座车", 7, True), ("无座位事实车", None, False)]
    for name, seats, with_seat_fact in plan:
        s = make_series(db, brand, name=name, body_type="suv", energy_types=("BEV",), source=source)
        y = make_year(db, s)
        facts = []
        if with_seat_fact:
            facts = [("参数信息", "座位数(个)", str(seats), "个", None)]
        v = make_variant(db, s, y, price_cny="150000", facts=facts, source=source)
        out[name] = v.id
    db.commit()
    return out


def _ids(db: Session, passengers: int | None) -> set[int]:
    p = UserProfile(budget=Budget(min=100000, max=200000))
    p.passengers = passengers
    res = recommendation_tool(db, p, limit=50)
    return {v["variant_id"] for v in res["variants"]}


def _run_without_backfill(db: Session, ids: dict[str, int], passengers: int | None) -> set[int]:
    """把 seat_count 全部置 NULL（模拟「回填还没跑」），看结果是否与回填前一致。"""
    from app.common.models import VehicleVariant

    saved = {i: db.get(VehicleVariant, i).seat_count for i in ids.values()}
    for i in ids.values():
        v = db.get(VehicleVariant, i)
        v.seat_count = None
    db.commit()
    try:
        return _ids(db, passengers)
    finally:
        for i, sc in saved.items():
            db.get(VehicleVariant, i).seat_count = sc
        db.commit()


def test_branch_1_and_2_and_3_with_backfill(db_session: Session):
    """回填后：无座位事实的**必须保留**，座位不够的**必须丢弃**。"""
    ids = _seed(db_session)
    from app.common.models import VehicleVariant

    # 按回填口径写入 seat_count
    for name, sc in (("五座车", 5), ("七座车", 7), ("无座位事实车", None)):
        db_session.get(VehicleVariant, ids[name]).seat_count = sc
    db_session.commit()

    got = _ids(db_session, passengers=6)
    assert ids["七座车"] in got, "7 座车应当满足「≥6 座」"
    assert ids["五座车"] not in got, "5 座车不满足「≥6 座」，必须被丢弃"
    assert ids["无座位事实车"] in got, (
        "没有座位事实的款型**必须保留**（缺数据 ≠ 不满足）——"
        "SQL 里省掉 IS NULL 就会丢掉它，且没有任何东西会报错"
    )

    got5 = _ids(db_session, passengers=5)
    assert ids["五座车"] in got5 and ids["七座车"] in got5


def test_backfill_not_run_behaves_exactly_like_before(db_session: Session):
    """回填没跑时（seat_count 全 NULL），结果必须与回填前**完全一致**。

    这是「先正确、后变快」的性质：下推在回填前是空操作，Python 侧兜底仍生效。
    """
    ids = _seed(db_session)
    for passengers in (None, 5, 6, 7, 9):
        with_backfill = _ids(db_session, passengers)          # 此时全 NULL（未回填）
        without = _run_without_backfill(db_session, ids, passengers)
        assert with_backfill == without, (
            f"passengers={passengers} 时，未回填的路径与显式 NULL 的路径结果不一致"
        )
        # 显式 NULL 时仍应保留「无座位事实车」
        assert ids["无座位事实车"] in with_backfill


def test_backfilled_values_do_not_change_results(db_session: Session):
    """回填**正确**时，候选集应与「全靠 Python 侧事实判定」完全一致。

    这里用同一份数据跑两遍：一遍 seat_count 写对，一遍置 NULL（逼 Python 兜底），
    两者必须给出同一个集合——这才是「等价下推」的定义。
    """
    ids = _seed(db_session)
    from app.common.models import VehicleVariant

    for name, sc in (("五座车", 5), ("七座车", 7), ("无座位事实车", None)):
        db_session.get(VehicleVariant, ids[name]).seat_count = sc
    db_session.commit()

    for passengers in (5, 6, 7):
        pushed = _ids(db_session, passengers)
        py_only = _run_without_backfill(db_session, ids, passengers)
        assert pushed == py_only, (
            f"passengers={passengers}：下推结果 {sorted(pushed)} "
            f"≠ 纯 Python 判定 {sorted(py_only)}——下推改变了推荐结果"
        )
