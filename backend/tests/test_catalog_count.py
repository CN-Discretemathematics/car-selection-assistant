# -*- coding: utf-8 -*-
"""全库盘点计数（2026-09-17 用户实测缺陷 + 独立评审整改的回归测试）。

背景：用户问「全部车型有多少款车？」，Agent 却把它当成选车需求，回了一句
「为了帮你挑到合适的车，先问一下：购车预算大概是多少？」——盘点/计数类提问
必须读库如实报数（与品牌盘点同一原则：数据正确性来自数据库，不来自措辞联想）。

评审（2026-09-17）抓到的三类问题各有回归：
- B1 品牌 + 计数（「奔驰有多少款车」）没被当作品牌约束 → 答成全库数；
- B2 修饰词夹在中间的问法（「有多少款新能源车」）漏识别 → 原症状仍在；
- B3 能源分桶用减法，把「能源未标注」当成新能源 → 向用户报假数字。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.engine import asks_catalog_count
from app.catalog.brands import catalog_overview
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session, *, unlabeled: bool = False) -> None:
    """2 品牌 / 3 在售车系（2 SUV + 1 轿车）/ 3 在售款型。

    `unlabeled=True` 时把轿车那条的 energy_types 置空，用于验证「未标注 ≠ 新能源」。
    """
    source = make_source(db, name="汽车之家")
    toyota = make_brand(db, name="丰田", source=source)
    byd = make_brand(db, name="比亚迪", source=source)
    for brand, name, body, energy, price in (
        (toyota, "凯美瑞", "sedan", "ICE", "200000"),
        (toyota, "汉兰达", "suv", "HEV", "280000"),
        (byd, "汉L", "suv", "PHEV", "259800"),
    ):
        series = make_series(
            db,
            brand,
            name=name,
            body_type=body,
            energy_types=() if (unlabeled and name == "凯美瑞") else (energy,),
            source=source,
        )
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
        # 评审 B2：修饰词夹在中间 / 省略名词 / 名词在前
        "有多少款新能源车",
        "有多少款SUV",
        "有多少款纯电车",
        "有多少款电动车",
        "现在在售车型一共几款",
        "车系有多少",
        "现在在售车系有几个",
        "SUV有多少款车",
        "奔驰有多少款车",
        "车型数量",
        # 「盘点」是数量问法的常见前缀：数量仍以确定性计数为准（工具循环只能看到截断结果）
        "盘点一下有多少款车",
    ):
        assert asks_catalog_count(text), f"应识别为盘点计数：{text}"


def test_asks_catalog_count_negative():
    for text in (
        "预算 20 万，家用 5 人",          # 选车约束
        "15 万以内有哪些车",              # 带约束的列举
        "凯美瑞怎么样",                    # 车系问答
        "这款车有多少个座位",              # 问配置项，不是盘点车系数量
        "哪款车有多少马力",                # 同上
        "一次充电能跑多少公里",            # 同上
        "汉兰达有几个版本",                # 同上
        "对比一下秦PLUS和海豹06",            # 对比
        # 评审建议 1：排名/解释类问法拿总数回答等于答非所问
        "哪个品牌车型数量最多",
        "帮我解释一下车型数量是什么意思",
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
    assert overview["unlabeled_series_count"] == 0
    assert overview["without_variants"] == 0


def test_catalog_overview_does_not_treat_missing_energy_as_new_energy(db_session: Session):
    """评审 B3 回归：能源未标注的车系既不算燃油也不算新能源（不许用减法）。"""
    _seed(db_session, unlabeled=True)
    overview = catalog_overview(db_session)
    assert overview["series_count"] == 3
    assert overview["fuel_series_count"] == 1        # 只剩汉兰达(HEV)
    assert overview["new_energy_series_count"] == 1  # 只剩汉L(PHEV)
    assert overview["unlabeled_series_count"] == 1   # 凯美瑞（未标注）单列


def test_catalog_overview_filters_by_body_and_energy(db_session: Session):
    """评审建议 2：带车身/能源的问法要答子集计数，不能答全库数。"""
    _seed(db_session)
    suv = catalog_overview(db_session, body_types=["suv"])
    assert suv["series_count"] == 2
    assert suv["variant_count"] == 2
    nev = catalog_overview(db_session, energy_allowed={"PHEV"})
    assert nev["series_count"] == 1
    fuel = catalog_overview(db_session, energy_allowed={"ICE", "HEV"})
    assert fuel["series_count"] == 2                 # ICE + HEV
    assert catalog_overview(db_session, body_types=["mpv"])["series_count"] == 0


def test_energy_count_question_uses_engine_vocabulary(client: TestClient, db_session: Session):
    """回归：profile 里的能源词是 new_energy/fuel 这类**泛化 token**，
    过滤前必须按引擎口径展开成具体类型，否则「有多少款新能源车」会答成 0。"""
    _seed(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "有多少款新能源车？"},
    ).json()
    text = out.get("explanation") or ""
    assert out["filters"].get("catalog_count") is True
    assert out["filters"].get("series_count") == 1, f"新能源车系应数到 1（汉L），实际：{out['filters']} {text}"
    assert "新能源" in text, f"应说明范围是新能源，实际：{text}"


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
    # 断言钉在计数本身：`"3" in text` 太松（款型数也是 3，series_count 退化成 0 也绿）
    assert out["filters"].get("catalog_count") is True, "应可观测地区分「读库盘点」与其它链路"
    assert out["filters"].get("series_count") == 3, f"车系数应来自库，实际 filters={out['filters']}"
    assert "3 个在售车系" in text, f"正文本应报出车系数，实际：{text}"


def test_brand_count_question_stays_brand_scoped(client: TestClient, db_session: Session):
    """评审 B1 回归：问「奔驰有多少款车」不能答成全库数字。"""
    _seed(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "丰田有多少款车？"},
    ).json()
    text = out.get("explanation") or ""
    assert out.get("filters", {}).get("catalog_count") is None, f"品牌计数不该走全库盘点：{text}"
    assert "丰田在售车型共 2 款" in text, f"应报该品牌的库内款数，实际：{text}"


def test_body_count_question_answered_for_subset(client: TestClient, db_session: Session):
    """评审建议 2 回归：带车身的计数答子集（2 个 SUV），不是全库 3 个。"""
    _seed(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "SUV有多少款车？"},
    ).json()
    text = out.get("explanation") or ""
    assert out["filters"].get("series_count") == 2, f"应只数 SUV，实际：{text}"
    assert "SUV" in text, f"范围应说明是 SUV，实际：{text}"


def test_constrained_count_falls_back_to_recommendation_chain(client: TestClient, db_session: Session):
    """守卫回归：带核心约束的计数不能被全库盘点劫持（仍走既有链路）。"""
    _seed(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "15万以内有多少款车？"},
    ).json()
    assert out.get("filters", {}).get("catalog_count") is None
