# -*- coding: utf-8 -*-
"""推荐链对悬空 series_id 的防御：一处脏数据不得让整个接口 500。

背景：款型的 series_id 可能指向已不存在的车系（导入期脏数据、车系下线但款型
残留）。series_map 按 `id.in_(...)` 构建，查不到的款型其 series 为 None，而
「维护便利性」维度会裸取 series.brand_id → AttributeError → 整个
POST /api/v1/recommendations 与 Agent 推荐链失败。

生产 SQLite 侧外键默认关闭、PG 侧导入期同样可能留下历史脏引用，故下游防御不可省。
本组用例显式关闭外键来构造悬空引用（与是否开启 PRAGMA 无关，环境免疫）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.schemas import UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

_DANGLING_SERIES_ID = 999_999


def _dangling(db_session: Session, variant) -> None:
    """把款型指向不存在的车系（SQLite/PG 两侧均可能存在的脏引用形态）。"""
    db_session.execute(text("PRAGMA foreign_keys=OFF"))
    variant.series_id = _DANGLING_SERIES_ID
    db_session.commit()
    db_session.execute(text("PRAGMA foreign_keys=ON"))


def test_dangling_series_id_is_skipped(db_session: Session):
    """悬空款型被跳过，其余款型照常参与推荐（修复前：整个接口 500）。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)

    ok_series = make_series(
        db_session, brand, name="正常车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    ok_variant = make_variant(
        db_session, ok_series, make_year(db_session, ok_series),
        config_version="标准版", energy_type="BEV", price_cny="150000", source=source,
    )

    dangling_series = make_series(
        db_session, brand, name="待下线车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    dangling_variant = make_variant(
        db_session, dangling_series, make_year(db_session, dangling_series),
        config_version="标准版", energy_type="BEV", price_cny="160000", source=source,
    )
    db_session.commit()

    _dangling(db_session, dangling_variant)

    result = recommendation_tool(db_session, UserProfile(), limit=10)
    ids = [v["variant_id"] for v in result["variants"]]

    assert ok_variant.id in ids, "正常款型应照常参与推荐"
    assert dangling_variant.id not in ids, "悬空款型必须被跳过，不得进入评分"
    assert result["count"] == 1


def test_all_candidates_dangling_returns_empty(db_session: Session):
    """全部候选都悬空时返回空结果而非 500（对外契约不被破坏）。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    series = make_series(
        db_session, brand, name="车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    variant = make_variant(
        db_session, series, make_year(db_session, series),
        config_version="标准版", energy_type="BEV", price_cny="150000", source=source,
    )
    db_session.commit()

    _dangling(db_session, variant)

    result = recommendation_tool(db_session, UserProfile(), limit=10)
    assert result["count"] == 0
    assert result["variants"] == []
