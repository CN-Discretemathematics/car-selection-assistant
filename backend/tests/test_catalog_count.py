# -*- coding: utf-8 -*-
"""全库盘点计数（2026-09-17 用户实测缺陷的回归测试）。

背景：用户问「全部车型有多少款车？」，Agent 却把它当成选车需求，回了一句
「为了帮你挑到合适的车，先问一下：购车预算大概是多少？」——盘点/计数类提问
必须读库如实报数（与品牌盘点同一原则：数据正确性来自数据库，不来自措辞联想）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.engine import asks_catalog_count
from app.catalog.brands import catalog_overview
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> None:
    """2 个品牌 / 3 个在售车系 / 3 个在售款型（1 燃油 + 1 油混 + 1 插混）。"""
    source = make_source(db, name="汽车之家")
    toyota = make_brand(db, name="丰田", source=source)
    byd = make_brand(db, name="比亚迪", source=source)
    for brand, name, energy, price in (
        (toyota, "凯美瑞", "ICE", "200000"),
        (toyota, "汉兰达", "HEV", "280000"),
        (byd, "汉L", "PHEV", "259800"),
    ):
        series = make_series(db, brand, name=name, energy_types=(energy,), source=source)
        year = make_year(db, series)
        make_variant(db, series, year, config_version="标准版", energy_type=energy,
                     price_cny=price, source=source)
    db.commit()


def test_asks_catalog_count_positive():
    for text in (
        "全部车型有多少款车？",
        "一共有多少款车",
        "现在有多少个车系",
        "库里有几款车型",
        "总共有多少款车",
        "车系总数是多少",
    ):
        assert asks_catalog_count(text), f"应识别为盘点计数：{text}"


def test_asks_catalog_count_negative():
    for text in (
        "预算 20 万，家用 5 人",          # 选车约束
        "15 万以内有哪些车",              # 带约束的列举
        "凯美瑞怎么样",                    # 车系问答
        "这款车有多少个座位",              # 问配置项，不是盘点车系数量
        "哪款车有多少马力",                # 同上
        "对比一下秦PLUS和海豹06",            # 对比
    ):
        assert not asks_catalog_count(text), f"不应识别为盘点计数：{text}"


def test_catalog_overview_counts_from_db(db_session: Session):
    _seed(db_session)
    overview = catalog_overview(db_session)
    assert overview["series_count"] == 3
    assert overview["variant_count"] == 3
    assert overview["brand_count"] == 2
    assert overview["fuel_series_count"] == 2        # 凯美瑞(ICE) + 汉兰达(HEV)
    assert overview["new_energy_series_count"] == 1  # 汉L(PHEV)


def test_catalog_count_question_answered_not_clarified(client: TestClient, db_session: Session):
    """核心回归：问「有多少款车」必须得到库内数量，而不是追问预算。"""
    _seed(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "全部车型有多少款车？"},
    ).json()
    text = out.get("explanation") or ""
    assert out["need_clarification"] is False, "盘点问题不应退化成追问"
    assert "先问一下" not in text and "购车预算大概" not in text, f"不该追问预算，实际：{text}"
    assert "3" in text, f"回复应含库内在售车系数 3，实际：{text}"
    assert out["filters"].get("catalog_count") is True, "应可观测地区分「读库盘点」与其它链路"
