"""`catalog.sales_ranking()` 与 `/home` 的口径必须逐条一致——**且这条测试要真的调 `/home`**。

2026-10-05 独立审查 P1-2：原 `test_sales_routing_new_branches.py` 里那条
`test_sales_ranking_matches_home_endpoint_caliber` 名字里写着 matches_home_endpoint，
**函数体从未调用过 `home()`**——只是把同一份期望顺序写了两遍，于是它证明的是
「助手榜等于它自己」。

审查用两种变异证明了这点（都全绿，59 passed）：
  M11 让 `/home` 单边加一条 `sales_ranking` 没有的过滤
  M12 让 `/home` 单边丢掉零售优先
也就是说 docstring 里「与 /home 同口径」这句承诺，在本文件之前**没有任何东西在守**。

本文件改为**真调 `/home` 接口**，把两边的 (series_id, sales_count) 序列逐位比对。
两份实现仍然各存一份（合并要动首页排名语义，属需单独拍板的重构），
但从此「同口径」是一条会变红的断言，而不是一句注释。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.catalog import services as catalog
from tests.seed import (
    make_brand,
    make_sales,
    make_series,
    make_source,
    make_variant,
    make_year,
)

MONTH = "2026-09"


def _seed_home_vs_ranking(db_session: Session) -> None:
    """构造一个**能区分各种口径**的库。

    刻意埋了四个陷阱，少任何一个这条测试就退化成「两边都返回同一堆」：
      ① 同车系同时有 retail(100) 与 portal(50000) → 口径必须取 retail 100
      ② 车系 active_status != active            → 必须被排除
      ③ 品牌 active_status != active            → 必须被排除
      ④ 只存在于上一个月份                      → 必须被排除（月份口径）
    另外放一个销量并列的车系，确保比对不依赖「唯一排序键」。
    """
    src = make_source(db_session, name="甲源")
    alive = make_brand(db_session, name="在售牌", source=src)
    dead_brand = make_brand(db_session, name="停牌", source=src)
    dead_brand.active_status = "inactive"

    rows = [
        # (品牌, 车系名, 车系状态, [(sales_type, 销量)])
        (alive, "双口径车", "active", [("retail", 100), ("portal", 50000)]),
        (alive, "正常车", "active", [("retail", 9000)]),
        (alive, "并列车", "active", [("retail", 9000)]),
        (alive, "停售车", "inactive", [("retail", 99999)]),
        (dead_brand, "停牌车", "active", [("retail", 88888)]),
        (alive, "旧月车", "active", [("retail", 77777)]),
    ]
    for brand, name, status, sales_rows in rows:
        series = make_series(db_session, brand, name=name, body_type="sedan",
                             energy_types=("BEV",), source=src)
        series.active_status = status
        year = make_year(db_session, series)
        make_variant(db_session, series, year, config_version="旗舰版",
                     energy_type="BEV", price_cny="200000", source=src,
                     facts=[("参数信息", "CLTC纯电续航里程(km)", "600", "km", "CLTC")])
        for sales_type, count in sales_rows:
            make_sales(db_session, series, MONTH,
                       count=int(count), source=src, sales_type=sales_type)
        if name == "旧月车":
            make_sales(db_session, series, "2026-08",
                       count=77777, source=src, sales_type="retail")
    db_session.commit()


def _home_pairs(client: TestClient) -> list[tuple[int, int]]:
    payload = client.get("/api/v1/home", params={"month": MONTH, "limit": 50}).json()
    return [(row["series_id"], row["sales_count"]) for row in payload]


def test_ranking_matches_home_endpoint_caliber(client: TestClient, db_session: Session):
    """助手侧销量榜与首页榜必须同源同序同数——真调 `/home` 逐位比对。

    与旧测试的区别：旧的那条只把期望序列写了两遍；这条的期望值来自 `/home` 本身，
    因此 `/home` 一改（加过滤、丢零售优先、换月份口径），本条立刻变红。
    """
    _seed_home_vs_ranking(db_session)
    ranking = catalog.sales_ranking(db_session, month=MONTH, limit=0)
    mine = [(series.id, sales.sales_count) for sales, series, _brand in ranking]
    assert mine == _home_pairs(client), "助手榜与 /home 不同源同序"


def test_ranking_picks_retail_over_portal(db_session: Session):
    """① 零售优先、门户回退（评审 M2）——docstring 点名的口径，此前零覆盖。"""
    _seed_home_vs_ranking(db_session)
    pairs = {
        series.name: sales.sales_count
        for sales, series, _brand in catalog.sales_ranking(db_session, month=MONTH, limit=0)
    }
    assert pairs["双口径车"] == 100, "同车系同时有零售与门户时必须取零售"


def test_ranking_excludes_inactive_series_and_brand(db_session: Session):
    """②③ 只统计车系/品牌都 active 的记录——docstring 点名的口径，此前零覆盖。"""
    _seed_home_vs_ranking(db_session)
    names = {
        series.name
        for _sales, series, _brand in catalog.sales_ranking(db_session, month=MONTH, limit=0)
    }
    assert "停售车" not in names, "车系 active_status != active 必须排除"
    assert "停牌车" not in names, "品牌 active_status != active 必须排除"


def test_ranking_only_counts_requested_month(db_session: Session):
    """④ 只取指定月份——跨月数据必须被排除。"""
    _seed_home_vs_ranking(db_session)
    names = {
        series.name
        for _sales, series, _brand in catalog.sales_ranking(db_session, month=MONTH, limit=0)
    }
    assert "旧月车" in names, "旧月车在本月也有数据，应在榜内"
    assert catalog.sales_ranking(db_session, month="2026-08", limit=0), "8 月亦应有数据"
    aug = {
        series.name
        for _sales, series, _brand in catalog.sales_ranking(db_session, month="2026-08", limit=0)
    }
    assert "正常车" not in aug, "只在本月有销量的车系不该出现在 8 月榜"


def test_ranking_filters_sales_type_to_retail_and_portal(db_session: Session):
    """⑤ 只统计 retail/portal 两种口径——docstring 点名，此前零覆盖。"""
    src = make_source(db_session, name="乙源")
    brand = make_brand(db_session, name="口径牌", source=src)
    series = make_series(db_session, brand, name="口径车", body_type="sedan",
                         energy_types=("BEV",), source=src)
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="旗舰版",
                 energy_type="BEV", price_cny="200000", source=src,
                 facts=[("参数信息", "CLTC纯电续航里程(km)", "600", "km", "CLTC")])
    # 某车系同时有 retail 与第三种口径：若不过滤 sales_type，portal/retail 的取舍会失真
    make_sales(db_session, series, MONTH, count=100, source=src, sales_type="retail")
    make_sales(db_session, series, MONTH, count=999999, source=src, sales_type="insurance")
    db_session.commit()

    picked = {
        series.name: sales.sales_count
        for sales, series, _brand in catalog.sales_ranking(db_session, month=MONTH, limit=0)
    }
    assert picked.get("口径车") == 100, (
        f"只应采信 retail/portal；insurance 口径的 999999 不该进榜（实际 {picked}）"
    )


def test_ranking_sorted_desc_regardless_of_limit(client: TestClient, db_session: Session):
    """⑥ 排名恒按销量降序（评审 P2）——升序查看时 rank=1 不应给销量垫底车型。"""
    _seed_home_vs_ranking(db_session)
    ranking = catalog.sales_ranking(db_session, month=MONTH, limit=0)
    counts = [sales.sales_count for sales, _s, _b in ranking]
    assert counts == sorted(counts, reverse=True), f"销量榜未按降序：{counts}"


def test_sales_ranking_citation_points_at_sales_source_not_brand_source(
    client: TestClient, db_session: Session
):
    """P0 回归：销量榜的引用必须挂在**销量记录**的来源上（2026-10-05 独立审查）。

    原实现写成 `for _s, _v, s in rows`，而 rows 元素是
    `(MonthlySales, VehicleSeries, Brand)`——解包后 `s` 绑到 **Brand**。
    `Brand` / `VehicleSeries` / `MonthlySales` **三者都有** `source_id`，
    于是 `getattr(..., None)` 的默认值形同虚设，引用被静默换成品牌名录的来源，
    却贴着「销量数据」标签。

    这条缺陷全仓测试无感（把品牌来源与销量来源对调，任何断言都不变），
    所以这里刻意用**三个不同来源**把它钉死。
    """
    brand_src = make_source(db_session, name="品牌名录来源")
    sales_src = make_source(db_session, name="销量数据来源")
    brand = make_brand(db_session, name="三源牌", source=brand_src)
    series = make_series(db_session, brand, name="三源车", body_type="sedan",
                         energy_types=("BEV",), source=brand_src)
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="旗舰版",
                 energy_type="BEV", price_cny="200000", source=brand_src,
                 facts=[("参数信息", "CLTC纯电续航里程(km)", "600", "km", "CLTC")])
    make_sales(db_session, series, MONTH, count=12345, source=sales_src,
               sales_type="retail")
    db_session.commit()

    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "什么车卖得好"},
    ).json()

    labels = {c.get("label") for c in (out.get("citations") or [])}
    names = {c.get("source_name") for c in (out.get("citations") or [])}
    assert labels, "销量榜回答必须带引用"
    assert brand_src.name not in names, (
        f"引用里出现了品牌名录来源（{brand_src.name}）——解包错位绑到了 Brand。"
        f"实际引用：{out.get('citations')}"
    )
    assert sales_src.name in names, (
        f"引用应指向销量数据来源（{sales_src.name}）。实际引用：{out.get('citations')}"
    )
