"""Agent 业务工具与 LLM 未配置路径测试。"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.agent.session import MAX_MESSAGES, SessionStore
from app.agent.tools import (
    TOOL_SCHEMAS,
    comparison_tool,
    official_link_tool,
    retrieval_search,
    sales_search,
    vehicle_search,
)
from app.catalog.services import latest_full_month
from app.common.llm import LLMClient, LLMError
from app.common.models import SourceDocument
from app.rag.service import reset_index
from tests.seed import make_brand, make_sales, make_series, make_source, make_variant, make_year


def _seed(db: Session):
    source = make_source(db, name="官方测试来源")
    brand = make_brand(db, name="测试品牌", source=source)
    suv = make_series(db, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    sedan = make_series(db, brand, name="通勤家轿", body_type="sedan", energy_types=("BEV",), source=source)
    y1, y2 = make_year(db, suv), make_year(db, sedan)
    v1 = make_variant(
        db, suv, y1, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[("动力", "power_kw", "150", "kW", None), ("电池和续航", "range_km", "605", "km", "CLTC")],
        source=source,
    )
    v2 = make_variant(
        db, suv, y1, config_version="长续航版", energy_type="BEV", price_cny="149800",
        facts=[("动力", "power_kw", "150千瓦", "千瓦", None), ("电池和续航", "range_km", "755", "km", "CLTC")],
        source=source,
    )
    make_variant(db, sedan, y2, config_version="标准版", energy_type="BEV", price_cny="99800", source=source)
    make_sales(db, suv, latest_full_month(), 1000, source=source)
    make_sales(db, sedan, latest_full_month(), 500, source=source)
    db.commit()
    return source, v1.id, v2.id


def test_vehicle_search_filters(db_session: Session):
    _seed(db_session)
    hits = vehicle_search(db_session, query="SUV")
    assert len(hits) == 1 and hits[0]["series_name"] == "家用SUV"

    hits = vehicle_search(db_session, body_type="sedan")
    assert [h["series_name"] for h in hits] == ["通勤家轿"]

    hits = vehicle_search(db_session, energy_type="new_energy")
    assert len(hits) == 2

    hits = vehicle_search(db_session, energy_type="ICE")
    assert hits == []


def test_sales_search_ranking(db_session: Session):
    _seed(db_session)
    rows = sales_search(db_session)
    assert rows[0]["series_name"] == "家用SUV"
    assert rows[1]["series_name"] == "通勤家轿"
    assert all(r["month"] == latest_full_month() for r in rows)


def test_sales_search_retail_first_portal_fallback(db_session: Session):
    """销量工具口径与首页一致：逐车系零售优先、门户回退（评审 M4）。"""
    from app.common.models import MonthlySales

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

    rows = sales_search(db_session, month="2026-07")
    by_name = {r["series_name"]: r for r in rows}
    assert set(by_name) == {"车A", "车B"}
    assert by_name["车A"]["sales_type"] == "retail"
    assert by_name["车B"]["sales_type"] == "portal"

    # 纯门户口径的月份也能返回
    db_session.add(MonthlySales(series_id=a.id, month="2026-06", sales_type="portal",
                                sales_count=50, source_id=source.id))
    db_session.commit()
    rows = sales_search(db_session, month="2026-06")
    assert rows and all(r["sales_type"] == "portal" for r in rows)


def test_comparison_tool_common_params(db_session: Session):
    _, v1, v2 = _seed(db_session)
    result = comparison_tool(db_session, [v1, v2])
    assert len(result["variants"]) == 2
    keys = {(p["category"], p["fact_key"]) for p in result["common_params"]}
    assert ("动力", "power_kw") in keys  # 150 kW 与 150千瓦 归一化后相同
    assert ("电池和续航", "range_km") not in keys  # 605 vs 755 不同


def test_official_link_tool(db_session: Session):
    _seed(db_session)
    from app.common.models import VehicleSeries

    series_id = db_session.query(VehicleSeries).first().id
    link = official_link_tool(db_session, series_id)
    assert link["official_page_url"] == "https://example.com/series"
    assert official_link_tool(db_session, 999999)["error"]


def test_tool_schemas_valid():
    assert len(TOOL_SCHEMAS) >= 6
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_retrieval_search_returns_evidence(db_session: Session):
    reset_index()
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    suv = make_series(db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db_session, suv)
    make_variant(
        db_session, suv, year, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[("座位数", "座位数(个)", "5", "座", None), ("尺寸", "长*宽*高(mm)", "4820*1900*1700", "mm", None)],
        source=source,
    )
    db_session.add(
        SourceDocument(
            series_id=suv.id, source_id=source.id, url="https://example.com/spec.pdf",
            source_type="official_doc",
            content_text="大五座家用SUV，空间充裕，适合家庭出行。",
            effective_from=date(2025, 1, 1),
        )
    )
    db_session.commit()

    hits = retrieval_search(db_session, "空间 座位", top_k=3)
    assert hits, "应命中结构化事实切片"
    assert all(h["text"] for h in hits)
    assert all(h["source_id"] == source.id for h in hits)


def test_llm_unconfigured_raises():
    client = LLMClient(api_key="")
    assert client.available is False
    with pytest.raises(LLMError):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    with pytest.raises(LLMError):
        asyncio.run(_drain_stream(client))


def test_safety_guard_negation_allowed():
    """否定表述不算违规（评审 L6）：「不包含购置税/保险」应放行，促销仍拦截。"""
    from app.agent.tools import safety_guard

    ok, problem = safety_guard("本站报价不包含购置税，也不包含保险费用")
    assert ok, problem
    ok, problem = safety_guard("现在下单立减 5000")
    assert not ok and problem
    ok, problem = safety_guard("提供 3 年免息贷款方案")
    assert not ok and problem  # 「免息贷款」是促销，必须拦截
    ok, problem = safety_guard("无条件免息贷款")
    assert not ok and problem  # 单字「无」不得放行促销语（复审 M-L6-1）


async def _drain_stream(client: LLMClient) -> None:
    async for _ in client.stream([{"role": "user", "content": "hi"}]):
        pass


def test_session_message_cap():
    store = SessionStore(ttl_seconds=3600)
    sid = store.create()
    for i in range(MAX_MESSAGES + 10):
        store.append_message(sid, "user", f"msg-{i}")
    history = store.history(sid)
    assert len(history) == MAX_MESSAGES
    assert history[-1]["content"] == f"msg-{MAX_MESSAGES + 9}"
