"""评测语料的**可移植性**：anchors 写的是行号，换库就错位（2026-10-03 实测发现）。

## 为什么要专门测它

`eval/questions.json` 由 `gen_eval_questions.py` 从**当时的数据库**分层抽样生成，
`anchors.series_id` / `anchors.variant_id` 写的是那个库的**行号**。行号不是数据。
换任何一个库（本地快照、另一个环境、重新导入的库）就全错位了。

错位的表现**极有欺骗性**：语料期望 `series_id=901`（英菲尼迪QX80），而本地导入后
901 同样存在、却指向「智己LS6」——于是检索做得再好也判不出相关，Hit@5 只有 0.03。
**看着像「检索能力崩了」，实际是标签指错了人。** 更糟的是它不会报错，只会安静地
给出一个看起来很专业的低分。

所以本文件锁三件事：
  1. id 与名称一致时**原样通过**（在原始库上零行为变化——这是兼容性的底线）；
  2. id 错位但问句里能找到车系名时**按名重解析**；
  3. 两者都不成立时**计入 stale 并告警**，宁可拒绝报数也不能让人把 0.03 当成回退。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from tests.seed import make_brand, make_series, make_source, make_variant, make_year
from tools.eval_rag import _portable_anchors


def _db(db_session: Session) -> None:
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    # 刻意让两车系**长度不同**，用于验证「取最长匹配」
    a = make_series(db_session, brand, name="英菲尼迪QX80", body_type="suv",
                    energy_types=("BEV",), source=source)
    b = make_series(db_session, brand, name="智己LS6", body_type="suv",
                    energy_types=("BEV",), source=source)
    ya = make_year(db_session, a)
    make_variant(db_session, a, ya, price_cny="300000", source=source)
    yb = make_year(db_session, b)
    make_variant(db_session, b, yb, price_cny="250000", source=source)
    db_session.commit()


def test_matching_id_is_left_alone(db_session: Session):
    """id 在本库存在且其名称出现在问句里 → 信任 id（原始库上零变化）。"""
    _db(db_session)
    from sqlalchemy import select

    from app.common.models import VehicleSeries

    q80 = db_session.scalar(select(VehicleSeries).where(VehicleSeries.name == "英菲尼迪QX80"))
    questions = [{"id": "q1", "text": "英菲尼迪QX80 怎么样", "anchors": {"series_id": q80.id}}]
    out, stats = _portable_anchors(db_session, questions)
    assert out[0]["anchors"]["series_id"] == q80.id
    assert stats["remapped"] == 0
    assert stats["stale"] == 0


def test_stale_id_is_remapped_by_series_name(db_session: Session):
    """id 错位（指向别的车系）但问句里有车系名 → 按名重解析到正确的那个。"""
    _db(db_session)
    from sqlalchemy import select

    from app.common.models import VehicleSeries

    ls6 = db_session.scalar(select(VehicleSeries).where(VehicleSeries.name == "智己LS6"))
    questions = [{
        "id": "q1",
        "text": "英菲尼迪QX80 怎么样",
        "anchors": {"series_id": ls6.id},   # 行号指向智己LS6，但问的是 QX80
    }]
    out, stats = _portable_anchors(db_session, questions)
    names = {s.id: s.name for s in db_session.scalars(select(VehicleSeries))}
    assert names[out[0]["anchors"]["series_id"]] == "英菲尼迪QX80"
    assert stats["remapped"] == 1
    assert stats["stale"] == 0


def test_unresolvable_anchor_is_reported_not_silently_kept(db_session: Session):
    """既对不上 id 也找不到名字 → 保持原样但计入 stale（调用方据此告警）。"""
    _db(db_session)
    from sqlalchemy import select

    from app.common.models import VehicleSeries

    ls6 = db_session.scalar(select(VehicleSeries).where(VehicleSeries.name == "智己LS6"))
    questions = [{
        "id": "q1",
        "text": "这台车到底怎么样",   # 问句里没有任何本库车系名
        "anchors": {"series_id": ls6.id},
    }]
    out, stats = _portable_anchors(db_session, questions)
    assert out[0]["anchors"]["series_id"] == ls6.id, "不该乱改——宁可保留原样也不瞎猜"
    assert stats["stale"] == 1
    assert stats["stale_ratio"] == 1.0
