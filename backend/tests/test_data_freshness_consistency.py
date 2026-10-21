"""卡片「更新」日期的口径一致性（2026-10-21 用户实测缺陷的回归测试）。

## 现象

用户在「全部车型」页看到：

    2026-09 销量 50,359 辆（榜单口径）      更新：2026-08-29

⇒ **自相矛盾**：销量是 9 月的，日期却停在 8-29。用户原话：
「全部车型的数据更新时间有误，仍显示为 8.29，但实际上这已经是 9 月的销量」。

## 根因：两个页面用了**两个不同的字段**，而标签都是「更新：」

| 页面 | 取的日期 |
| --- | --- |
| 首页 / 销量榜（`app/sales/router.py`） | `sales.last_verified_at or series.last_verified_at`（**销量记录**的核验日） |
| 全部车型页（`app/vehicles/router.py`） | 只有 `series.last_verified_at`（**车系自己**的核验日，**早于**它显示的销量月份） |

生产实测：1102 个在售车系里有 **491 个**的「更新」日期早于它显示的销量月份。

## 本文件钉的三条不变量

1. 卡片上的「更新」**不得早于**它同时显示的销量月份；
2. 同一个车系，首页卡与列表卡的「更新」**必须一致**；
3. 详情页的「数据更新」与列表卡**必须一致**。

⇒ 修法是**口径统一**（一个函数写一处），不是给每个页面各补一个日期。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.seed import (
    make_brand,
    make_sales,
    make_series,
    make_source,
    make_variant,
    make_year,
)

#: 车系自己的核验日（早于它显示的销量月份 ⇒ 用户看到的就是这个「8-29」）
SERIES_VERIFIED = datetime(2026, 8, 29, 2, 30, tzinfo=timezone.utc)
#: 销量记录的核验日（首页/销量榜口径 ⇒ 「10-13」）
SALES_VERIFIED = datetime(2026, 10, 13, 2, 30, tzinfo=timezone.utc)
SALES_MONTH = "2026-09"


def _seed_gap(db_session: Session):
    """造一个「车系核验日（8-29）早于它显示的销量月份（2026-09）」的车系。"""
    source = make_source(db_session, name="新鲜度测试来源")
    brand = make_brand(db_session, name="新鲜度测试品牌", source=source)
    series = make_series(
        db_session, brand, name="新鲜度测试车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV",
        price_cny="150000", source=source,
    )
    series.last_verified_at = SERIES_VERIFIED
    db_session.flush()
    sales = make_sales(db_session, series, month=SALES_MONTH, count=50359, source=source)
    sales.last_verified_at = SALES_VERIFIED
    db_session.commit()
    return series


def _list_item(client: TestClient) -> dict:
    body = client.get("/api/v1/vehicles", params={"page_size": 50}).json()
    items = body["items"]
    assert items, "列表页没有返回任何车系"
    return next(i for i in items if i["series_name"] == "新鲜度测试车系")


def test_list_card_updated_at_is_not_older_than_the_sales_it_shows(
    client: TestClient, db_session: Session
):
    """不变量 1：卡片上的「更新」**不得早于**它同时显示的销量月份。

    修之前：列表页取车系核验日 2026-08-29 < 2026-09 ⇒ 矛盾 ⇒ 本用例红。
    """
    _seed_gap(db_session)
    item = _list_item(client)
    assert item["latest_sales"]["month"] == SALES_MONTH, item
    assert item["data_updated_at"] is not None, "卡片必须给出更新时间"
    updated = item["data_updated_at"][:10]
    assert updated >= f"{SALES_MONTH}-01", (
        f"更新日期 {updated} 早于它显示的销量月份 {SALES_MONTH} ⇒ 卡片自相矛盾"
    )


def test_home_card_and_list_card_show_the_same_updated_at(
    client: TestClient, db_session: Session
):
    """不变量 2：同一个车系，首页卡与列表卡的「更新」必须一致。

    修之前：首页取销量核验日（10-13）、列表取车系核验日（08-29）⇒ 不一致 ⇒ 本用例红。
    """
    _seed_gap(db_session)
    home = client.get("/api/v1/home").json()
    card = next(c for c in home if c["series_name"] == "新鲜度测试车系")
    item = _list_item(client)
    assert card["data_updated_at"] == item["data_updated_at"], (
        f"同一车系两个页面的更新日期不一致：首页 {card['data_updated_at']}、"
        f"列表 {item['data_updated_at']}"
    )


def test_detail_page_updated_at_matches_the_list(
    client: TestClient, db_session: Session
):
    """不变量 3：详情页的「数据更新」与列表卡一致。"""
    series = _seed_gap(db_session)
    detail = client.get(f"/api/v1/vehicles/{series.id}").json()
    item = _list_item(client)
    assert detail["data_updated_at"] == item["data_updated_at"], (
        f"详情页 {detail['data_updated_at']} 与列表 {item['data_updated_at']} 不一致"
    )
