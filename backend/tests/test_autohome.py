"""汽车之家数据管线测试（解析/载荷/外部 ID 归并/门户口径回退/车系详情）。"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import Brand, ExternalSeriesRef, MonthlySales, VehicleSeries
from app.sources.autohome import (
    build_payload,
    build_series_payload,
    format_price_note,
    map_body_type,
    map_energy_types,
    parse_rank_page,
    parse_series_page,
)
from app.sources.importer import import_catalog
from tests.seed import make_brand, make_series, make_source, make_year


def _fixture_html(rows: list[dict], date: str = "2026-06") -> str:
    data = {
        "props": {
            "pageProps": {
                "initialValues": {"date": date},
                "listRes": {"list": rows},
            }
        }
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data, ensure_ascii=False)
        + "</script>"
    )


def _sample_rows() -> list[dict]:
    return [
        {"rankNum": 1, "seriesid": "7806", "seriesname": "Model Y", "salecount": 38654},
        {"rankNum": 2, "seriesid": "8888", "seriesname": "示例家轿", "salecount": 24865},
        {"rankNum": 3, "seriesid": "9999", "seriesname": "无销量条目", "salecount": "N/A"},
    ]


def test_parse_rank_page():
    parsed = parse_rank_page(_fixture_html(_sample_rows()))
    assert parsed["month"] == "2026-06"
    assert len(parsed["rows"]) == 2  # salecount 非数字的条目被跳过
    assert parsed["rows"][0] == {
        "rank": 1,
        "external_id": "7806",
        "seriesname": "Model Y",
        "salecount": 38654,
    }


def test_parse_rank_page_missing_data():
    try:
        parse_rank_page("<html>no data</html>")
        raise AssertionError("应抛出 ValueError")
    except ValueError:
        pass


def test_build_payload():
    payload = build_payload(
        parse_rank_page(_fixture_html(_sample_rows())),
        "https://www.autohome.com.cn/rank/1-1-0-0_9000-x-x-x/2026-06.html",
    )
    assert payload["source"]["source_type"] == "industry_data"
    assert payload["brands"][0]["name"] == "待分类（汽车之家销量榜）"
    assert payload["series"][0]["external_id"] == "7806"
    assert payload["sales"][0]["sales_type"] == "portal"
    assert payload["sales"][0]["count"] == 38654


def test_import_autohome_roundtrip(db_session: Session):
    payload = build_payload(
        parse_rank_page(_fixture_html(_sample_rows())),
        "https://www.autohome.com.cn/rank/1-1-0-0_9000-x-x-x/2026-06.html",
    )
    report = import_catalog(db_session, payload)
    assert report.ok, report.errors

    refs = db_session.scalars(select(ExternalSeriesRef)).all()
    assert len(refs) == 2
    sales = db_session.scalars(select(MonthlySales)).all()
    assert len(sales) == 2
    assert all(s.sales_type == "portal" for s in sales)

    # 重复导入：外部 ID 归并，不产生重复车系/销量
    report2 = import_catalog(db_session, payload)
    assert report2.ok
    assert len(db_session.scalars(select(VehicleSeries)).all()) == 2
    assert len(db_session.scalars(select(MonthlySales)).all()) == 2


def _series_fixture_html() -> str:
    data = {
        "props": {
            "pageProps": {
                "seriesBaseInfo": {
                    "id": 7806,
                    "name": "Model Y",
                    "brandName": "特斯拉",
                    "levelName": "中型SUV",
                    "minPrice": 263500,
                    "maxPrice": 313500,
                    "logo": "https://car3.autoimg.cn/x.png",
                    "fueltypes": "4",
                    "energytype": 1,
                }
            }
        }
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data, ensure_ascii=False)
        + "</script>"
    )


def test_parse_series_page():
    row = parse_series_page(_series_fixture_html(), "7806")
    assert row["external_id"] == "7806"
    assert row["brand_name"] == "特斯拉"
    assert row["level"] == "中型SUV"
    assert row["min_price"] == 263500


def test_energy_body_price_mapping():
    assert map_energy_types("4", 1) == ["BEV"]
    assert map_energy_types("4,5", 1) == ["BEV", "PHEV", "EREV"]
    assert map_energy_types("1,3", 0) == ["ICE", "HEV"]
    assert map_energy_types("1", 0) == ["ICE"]
    assert map_body_type("中型SUV") == "suv"
    assert map_body_type("紧凑型车") == "sedan"
    assert map_body_type("MPV") == "mpv"
    assert map_body_type("皮卡") == "pickup"
    assert map_body_type("轻客") is None  # 商用车不入乘用车枚举
    assert format_price_note(263500, 313500) == "26.35-31.35万元"
    assert format_price_note(99800, 99800) == "9.98万元"


def test_series_detail_import_moves_brand(db_session: Session):
    """榜单先入（待分类品牌）→ 车系详情导入时按外部 ID 归并并迁移到真实品牌。"""
    rank_payload = build_payload(
        parse_rank_page(_fixture_html(_sample_rows()[:1])),
        "https://www.autohome.com.cn/rank/x.html",
    )
    assert import_catalog(db_session, rank_payload).ok

    detail_payload = build_series_payload(
        [parse_series_page(_series_fixture_html(), "7806")],
        "https://www.autohome.com.cn/7806/",
    )
    report = import_catalog(db_session, detail_payload)
    assert report.ok, report.errors

    series = db_session.scalars(select(VehicleSeries).where(VehicleSeries.name == "Model Y")).first()
    assert series is not None
    brand = db_session.get(Brand, series.brand_id)
    assert brand.name == "特斯拉"
    assert series.price_range_note == "26.35-31.35万元"
    assert series.thumbnail_url == "https://car3.autoimg.cn/x.png"
    assert series.energy_types == ["BEV"]
    # 待分类品牌仍在（可能还有别的车系），真实品牌已建
    assert db_session.scalar(select(Brand).where(Brand.name == "待分类（汽车之家销量榜）")) is not None


def test_home_portal_fallback(client, db_session: Session):
    """门户榜单口径（无零售数据）也能在首页展示并标注口径。"""
    from fastapi.testclient import TestClient

    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="待分类（汽车之家销量榜）", source=source)
    series = make_series(db_session, brand, name="Model Y", source=source)
    make_year(db_session, series)
    db_session.add(
        MonthlySales(
            series_id=series.id,
            month="2026-06",
            sales_type="portal",
            sales_count=38654,
            source_id=source.id,
        )
    )
    db_session.commit()

    resp = client.get("/api/v1/home", params={"month": "2026-06"})
    assert resp.status_code == 200
    cards = resp.json()
    assert len(cards) == 1
    assert cards[0]["sales_type"] == "portal"
    assert cards[0]["sales_count"] == 38654
