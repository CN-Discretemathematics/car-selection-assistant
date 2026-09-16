"""核心 API 集成测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.catalog.services import latest_full_month
from app.common.models import MonthlySales, VehicleVariant
from tests.seed import (
    make_brand,
    make_external_ref,
    make_sales,
    make_series,
    make_source,
    make_variant,
    make_year,
)


def test_health(client: TestClient):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_vehicle_list_browse(client: TestClient, db_session: Session):
    """全部车型浏览：筛选/排序/分页/缩略图代理。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="特斯拉", source=source)
    suv = make_series(db_session, brand, name="Model Y", body_type="suv", energy_types=("BEV",), source=source)
    suv.thumbnail_url = "https://car3.autoimg.cn/x.png"
    year = make_year(db_session, suv)
    make_variant(db_session, suv, year, config_version="标准版", energy_type="BEV", price_cny="263500")
    # 停售旧款价格更低：不得进入价格区间（评审 P1：与 /home、详情页口径一致）
    stopped = make_variant(db_session, suv, year, config_version="停售旧款", energy_type="BEV", price_cny="50000")
    stopped.status = "discontinued"
    sedan = make_series(db_session, brand, name="Model 3", body_type="sedan", energy_types=("BEV",), source=source)
    year2 = make_year(db_session, sedan)
    make_variant(db_session, sedan, year2, config_version="标准版", energy_type="BEV", price_cny="235500")
    db_session.commit()

    body = client.get("/api/v1/vehicles").json()
    assert body["total"] >= 2
    assert body["items"][0]["thumbnail_url"].startswith("/api/v1/images/proxy?url=")
    suv_item = next(i for i in body["items"] if i["series_name"] == "Model Y")
    assert suv_item["price_range"]["min"] == 263500.0, "停售款价格不得计入区间下限"

    items = client.get("/api/v1/vehicles", params={"body_type": "sedan"}).json()["items"]
    assert items and all(i["body_type"] == "sedan" for i in items)

    prices = [i["price_range"]["min"] for i in client.get(
        "/api/v1/vehicles", params={"sort": "price_asc"}).json()["items"]]
    assert prices == sorted(prices)

    # 越界页码钳制到最后一页（评审 P2：此前返回空列表不友好）
    out_of_range = client.get("/api/v1/vehicles", params={"page": 999}).json()
    assert out_of_range["items"] and out_of_range["page"] < 999

    items = client.get("/api/v1/vehicles", params={"energy_type": "new_energy"}).json()["items"]
    assert items and all(any(t in ("BEV", "PHEV", "EREV") for t in i["energy_types"]) for i in items)

    items = client.get("/api/v1/vehicles", params={"price_max": 240000}).json()["items"]
    assert items and all(i["price_range"]["min"] is not None and i["price_range"]["min"] <= 240000 for i in items)


def test_brands_empty_then_seeded(client: TestClient, db_session: Session):
    assert client.get("/api/v1/brands").json() == []

    source = make_source(db_session)
    make_brand(db_session, name="示例品牌A", source=source)
    make_brand(db_session, name="示例品牌B", brand_type="luxury", source=source)
    db_session.commit()

    body = client.get("/api/v1/brands").json()
    assert len(body) == 2
    assert body[0]["name"] == "示例品牌A"
    assert body[1]["brand_type"] == "luxury"


def _seed_catalog(db_session: Session):
    source = make_source(db_session)
    brand = make_brand(db_session, name="测试品牌", source=source)
    series = make_series(db_session, brand, name="测试车系", energy_types=("BEV", "PHEV"), source=source)
    year = make_year(db_session, series, "2025款")
    v_low = make_variant(
        db_session, series, year, config_version="入门版", price_cny="129800",
        facts=[("动力", "power_kw", "150", "kW", None), ("电池和续航", "range_km", "500", "km", "CLTC")],
        source=source,
    )
    v_high = make_variant(
        db_session, series, year, config_version="旗舰版", price_cny="169800",
        facts=[("动力", "power_kw", "150千瓦", "千瓦", None), ("电池和续航", "range_km", "600", "km", "CLTC")],
        source=source,
    )
    make_sales(db_session, series, latest_full_month(), 32518, source=source)
    db_session.commit()
    return source, brand, series, year, v_low, v_high


def test_vehicle_detail(client: TestClient, db_session: Session):
    _, _, series, year, v_low, v_high = _seed_catalog(db_session)

    body = client.get(f"/api/v1/vehicles/{series.id}").json()
    assert body["name"] == "测试车系"
    assert body["brand"]["name"] == "测试品牌"
    assert body["price_range"]["min"] == 129800.0
    assert body["price_range"]["max"] == 169800.0
    assert body["latest_sales"]["sales_count"] == 32518
    assert body["latest_sales"]["month"] == latest_full_month()
    assert body["model_years"][0]["year_name"] == "2025款"

    assert client.get("/api/v1/vehicles/999999").status_code == 404


def test_vehicle_detail_source_entry(client: TestClient, db_session: Session):
    """跳转入口的优先级：官方车型页优先；官方缺失（线上真实形态）时回退数据来源页。

    背景：线上 `official_page_url` 全为空（来源不提供该字段），官方链接按钮恒不渲染；
    改为缺失时展示来源页入口——只认已知模板，未知来源/可疑 id 一律不给链接（不猜 URL）。
    优先级放在后端判定，这条测试就是它的门禁（前端只按 kind 选标签）。
    """
    source = make_source(db_session, name="汽车之家", source_type="industry_data")
    brand = make_brand(db_session, name="示例品牌", source=source)
    official = make_series(db_session, brand, name="有官方链接", source=source)      # 种子带 example.com
    fallback = make_series(db_session, brand, name="无官方链接", source=source)
    none_at_all = make_series(db_session, brand, name="两者都无", source=source)
    fallback.official_page_url = None       # 线上真实形态：官方链接为空
    none_at_all.official_page_url = None
    make_external_ref(db_session, official, external_id="220", source=source)
    make_external_ref(db_session, fallback, external_id="110", source=source)

    body = client.get(f"/api/v1/vehicles/{official.id}").json()
    assert body["external_link"] == {
        "kind": "official", "url": "https://example.com/series", "source_name": None
    }, body["external_link"]

    body = client.get(f"/api/v1/vehicles/{fallback.id}").json()
    assert body["official_page_url"] is None
    assert body["external_link"] == {
        "kind": "source", "url": "https://www.autohome.com.cn/110/", "source_name": "汽车之家"
    }, body["external_link"]

    body = client.get(f"/api/v1/vehicles/{none_at_all.id}").json()
    assert body["external_link"] is None

    # 有映射但外部 id 形态可疑（全角数字）：同样不给链接，绝不猜 URL
    suspicious = make_series(db_session, brand, name="id 可疑", source=source)
    suspicious.official_page_url = None
    make_external_ref(db_session, suspicious, external_id="１１０", source=source)
    assert client.get(f"/api/v1/vehicles/{suspicious.id}").json()["external_link"] is None


def test_source_page_url_only_for_known_sources_and_sane_ids():
    """未知来源、缺参数、可疑外部 id 一律不给链接（不猜 URL）。"""
    from app.sources.page_urls import source_page_url

    assert source_page_url("汽车之家", "110") == "https://www.autohome.com.cn/110/"
    assert source_page_url("某未知来源", "110") is None
    assert source_page_url("汽车之家", None) is None
    assert source_page_url("汽车之家", "") is None
    assert source_page_url(None, "110") is None
    # 形态可疑的 id 不拼链接（避免坏链接/越界路径）
    assert source_page_url("汽车之家", "  110/ ../x  ") is None
    assert source_page_url("汽车之家", "110x") is None


def test_vehicle_variants(client: TestClient, db_session: Session):
    _, _, series, _, v_low, v_high = _seed_catalog(db_session)

    body = client.get(f"/api/v1/vehicles/{series.id}/variants").json()
    assert len(body) == 2
    by_config = {v["config_version"]: v for v in body}
    assert by_config["入门版"]["official_price"]["price_cny"] == 129800.0
    # 归一化：150 kW 与 150千瓦 展示统一为 "150 kW"（单位去重，见 11.3）
    assert by_config["入门版"]["spec_facts"][0]["display"] == "150 kW"
    assert by_config["旗舰版"]["spec_facts"][0]["display"] == "150 kW"

    # 能源筛选（两个均为 BEV，筛选 HEV 返回空）；非法枚举返回 422
    filtered = client.get(f"/api/v1/vehicles/{series.id}/variants", params={"energy_type": "HEV"}).json()
    assert filtered == []
    assert client.get(f"/api/v1/vehicles/{series.id}/variants", params={"energy_type": "HYDROGEN"}).status_code == 422


def test_home_sorted_and_filters(client: TestClient, db_session: Session):
    source = make_source(db_session)
    brand = make_brand(db_session, source=source)
    s_a = make_series(db_session, brand, name="车系A", body_type="suv", energy_types=("BEV",), source=source)
    s_b = make_series(db_session, brand, name="车系B", body_type="sedan", energy_types=("ICE",), source=source)
    year_a, year_b = make_year(db_session, s_a), make_year(db_session, s_b)
    make_variant(db_session, s_a, year_a, price_cny="150000", source=source)
    make_variant(db_session, s_b, year_b, price_cny="100000", energy_type="ICE", source=source)
    month = latest_full_month()
    make_sales(db_session, s_a, month, 1000, source=source)
    make_sales(db_session, s_b, month, 5000, source=source)
    db_session.commit()

    body = client.get("/api/v1/home").json()
    assert [c["series_name"] for c in body] == ["车系B", "车系A"]
    assert body[0]["rank"] == 1
    assert body[0]["sales_count"] == 5000
    assert body[0]["price_range"]["min"] == 100000.0

    # 月份过滤（非法格式返回 422）
    assert client.get("/api/v1/home", params={"month": "2020-01"}).json() == []
    assert client.get("/api/v1/home", params={"month": "202001"}).status_code == 422

    # 能源过滤：HEV 归燃油侧
    fuel = client.get("/api/v1/home", params={"energy_type": "fuel"}).json()
    assert [c["series_name"] for c in fuel] == ["车系B"]

    # 价格过滤
    priced = client.get("/api/v1/home", params={"price_max": 120000}).json()
    assert [c["series_name"] for c in priced] == ["车系B"]

    # 升序
    asc = client.get("/api/v1/home", params={"sort": "asc"}).json()
    assert [c["series_name"] for c in asc] == ["车系A", "车系B"]


def test_home_defaults_to_latest_data_month(client: TestClient, db_session: Session):
    """生产故障回归：销量数据月滞后于「最近完整自然月」时（如月初），
    首页默认取库内最新数据月，而不是整体为空（2026-09-02 首页 Top20 消失）。"""
    source = make_source(db_session)
    brand = make_brand(db_session, source=source)
    series = make_series(db_session, brand, name="旧数据车系", energy_types=("BEV",), source=source)
    year = make_year(db_session, series)
    make_variant(db_session, series, year, price_cny="200000", source=source)
    old_month = "2024-01"  # 远早于 latest_full_month()，模拟数据发布滞后
    make_sales(db_session, series, old_month, 321, source=source)
    db_session.commit()

    body = client.get("/api/v1/home").json()
    assert body, "首页应按库内最新数据月回退展示，而不是返回空列表"
    assert body[0]["series_name"] == "旧数据车系"
    assert body[0]["month"] == old_month


def test_comparison_lifecycle_and_common_params(client: TestClient, db_session: Session):
    _, _, series, _, v_low, v_high = _seed_catalog(db_session)

    created = client.post("/api/v1/comparisons", json={"variant_ids": [v_low.id, v_high.id]})
    assert created.status_code == 201
    comparison_id = created.json()["id"]

    body = client.get(f"/api/v1/comparisons/{comparison_id}").json()
    assert body["variant_ids"] == [v_low.id, v_high.id]
    assert len(body["variants"]) == 2
    # 对比场景不展示外部跳转入口：响应里不得出现官方车型页/来源页字段
    # （口径见 skills/sku-comparison.md；入口只在详情页）
    assert all("official_page_url" not in v for v in body["variants"])
    assert all("external_link" not in v and "source_page_url" not in v for v in body["variants"])
    # 150 kW 与 150千瓦 归一化后相同 → 隐藏相同参数
    common_keys = {(p["category"], p["fact_key"]) for p in body["common_params"]}
    assert ("动力", "power_kw") in common_keys
    # 续航不同（500 vs 600）→ 不在公共参数里
    assert ("电池和续航", "range_km") not in common_keys

    # 幂等校验与错误分支
    assert client.get("/api/v1/comparisons/999999").status_code == 404
    assert client.post("/api/v1/comparisons", json={"variant_ids": [999999]}).status_code == 404
    assert client.post("/api/v1/comparisons", json={"variant_ids": []}).status_code == 422
    assert client.post("/api/v1/comparisons", json={"variant_ids": list(range(1, 7))}).status_code == 422

    # 已停售 SKU 不能加入对比（创建时校验，与读取侧跳过规则一致）
    off_sale = db_session.get(VehicleVariant, v_low.id)
    assert off_sale is not None
    off_sale.status = "off_sale"
    db_session.commit()
    resp = client.post("/api/v1/comparisons", json={"variant_ids": [v_low.id]})
    assert resp.status_code == 400


def test_comparison_ai_comment_shape(client: TestClient, db_session: Session, monkeypatch):
    """AI 点评端点契约：LLM 不可用/越界时 `ai_comment=None`（前端回退确定性 verdict）。"""
    import app.comparison.router as router_mod

    _, _, series, _, v_low, v_high = _seed_catalog(db_session)
    async def _none(analysis):
        return None

    monkeypatch.setattr(router_mod, "ai_comment_for", _none)
    resp = client.post("/api/v1/comparisons/analysis/ai-comment",
                       json={"variant_ids": [v_low.id, v_high.id]})
    assert resp.status_code == 200
    assert resp.json() == {"ai_comment": None}

    # 款型不足 2 个 → 400（与分析主端点同口径）
    resp = client.post("/api/v1/comparisons/analysis/ai-comment", json={"variant_ids": [v_low.id]})
    assert resp.status_code == 400


def test_source_get(client: TestClient, db_session: Session):
    source = make_source(db_session, name="乘联会")
    db_session.commit()
    body = client.get(f"/api/v1/sources/{source.id}").json()
    assert body["name"] == "乘联会"
    assert client.get("/api/v1/sources/999999").status_code == 404


def test_home_per_series_sales_fallback(client: TestClient, db_session: Session):
    """首页销量口径逐车系回退：有零售的车系用零售，只有门户的车系不消失（评审 M2）。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    a = make_series(db_session, brand, name="车A", source=source)
    b = make_series(db_session, brand, name="车B", source=source)
    make_year(db_session, a)
    make_year(db_session, b)
    db_session.add(MonthlySales(series_id=a.id, month="2026-07", sales_type="retail",
                                sales_count=100, source_id=source.id))
    db_session.add(MonthlySales(series_id=b.id, month="2026-07", sales_type="portal",
                                sales_count=200, source_id=source.id))
    db_session.commit()

    cards = client.get("/api/v1/home", params={"month": "2026-07"}).json()
    by_name = {c["series_name"]: c for c in cards}
    assert set(by_name) == {"车A", "车B"}, "两个车系都应出现在首页（逐车系回退而非全局回退）"
    assert by_name["车A"]["sales_type"] == "retail"
    assert by_name["车B"]["sales_type"] == "portal"


def test_home_asc_rank_follows_sales(client: TestClient, db_session: Session):
    """评审 P2：升序查看时 rank 仍按销量排（第 1 名奖牌不得戴给销量垫底车型）。"""
    source = make_source(db_session)
    brand = make_brand(db_session, source=source)
    s_high = make_series(db_session, brand, name="高销量车", source=source)
    s_low = make_series(db_session, brand, name="低销量车", source=source)
    year_h, year_l = make_year(db_session, s_high), make_year(db_session, s_low)
    make_variant(db_session, s_high, year_h, price_cny="100000", source=source)
    make_variant(db_session, s_low, year_l, price_cny="100000", source=source)
    month = latest_full_month()
    make_sales(db_session, s_high, month, 5000, source=source)
    make_sales(db_session, s_low, month, 1000, source=source)
    db_session.commit()

    body = client.get("/api/v1/home", params={"sort": "asc"}).json()
    assert [c["series_name"] for c in body] == ["低销量车", "高销量车"]
    assert body[0]["rank"] == 2 and body[1]["rank"] == 1, "排名应按销量而非列表顺序"


def test_comparison_idempotent_for_same_sku_set(client: TestClient, db_session: Session):
    """评审 P1：同组 SKU 重复创建（分享链接反复打开）复用同一对比行。"""
    _, _, series, _, v_low, v_high = _seed_catalog(db_session)
    assert series  # noqa: B018
    first = client.post("/api/v1/comparisons", json={"variant_ids": [v_low.id, v_high.id]})
    second = client.post("/api/v1/comparisons", json={"variant_ids": [v_high.id, v_low.id]})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"], "同组 SKU 重复创建应复用同一对比（与顺序无关）"

    third = client.post("/api/v1/comparisons", json={"variant_ids": [v_low.id]})
    assert third.json()["id"] != first.json()["id"], "不同 SKU 组合应创建新对比"
