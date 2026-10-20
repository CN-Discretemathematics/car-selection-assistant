"""版本差异回答要**尊重能源限定**（2026-10-21）。

## 缺陷（实测，单轮题库）

「享界S9 的**纯电版**有哪些款型」——版本差异路径此前**无视**能源限定，
把整个车系（在售 + 增程 + 插混）都列了出来 ⇒ 用户问纯电版、答案里混着增程版
（题库 q0024 / q0070 / q0078 三题都栽在这里 ⇒ 硬约束违规）。

修法：能源限定取自**画像**（唯一来源是 `extract_hints` 的 `energy_preference`），
版本清单只列该能源的款型；一款都没有就**如实说没有**，不把别的能源端上来。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.series_qa import build_variant_diff_answer
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_two_energies(db_session: Session):
    """同一个车系下种两个款型：纯电、增程。

    ⚠️ 「能源未标注」的款型**造不出来**（`vehicle_variants.energy_type` 是 NOT NULL）⇒
    过滤里的「能源未标注 ⇒ 保留」是**防御性**分支（本仓原则：缺数据 ≠ 不满足），
    不是可达路径。
    """
    source = make_source(db_session, name="能源限定测试来源")
    brand = make_brand(db_session, name="能源限定品牌", source=source)
    series = make_series(
        db_session, brand, name="能源限定车系", body_type="sedan",
        energy_types=("BEV", "EREV"), source=source,
    )
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="纯电版", energy_type="BEV",
        price_cny="150000", source=source,
    )
    make_variant(
        db_session, series, year, config_version="增程版", energy_type="EREV",
        price_cny="160000", source=source,
    )
    db_session.commit()
    return series, brand


def test_energy_filter_keeps_only_the_asked_energy(db_session: Session):
    """给了能源限定 ⇒ 版本清单里**只有**那一种能源（+ 能源未标注的款型）。"""
    series, brand = _seed_two_energies(db_session)

    text, rows = build_variant_diff_answer(
        db_session, series, brand, "有哪些款型", energy_preference=["BEV"]
    )

    assert rows, "应当给出纯电版那一款"
    assert all(r["energy_type"] == "BEV" for r in rows), rows
    assert "增程版" not in text, text


def test_without_energy_filter_all_variants_are_listed(db_session: Session):
    """**没给**能源限定 ⇒ 行为与从前逐位相同（所有能源都列）。"""
    series, brand = _seed_two_energies(db_session)

    _text, rows = build_variant_diff_answer(db_session, series, brand, "有哪些款型")

    assert len(rows) == 2, rows


def test_engine_passes_the_profile_energy_into_the_variant_list(db_session: Session):
    """**端到端**：问「纯电版有哪些款型」⇒ 回答里不得出现增程版。

    这一条钉的是**接线**（画像 → 版本路径）：只测工具层的话，
    「引擎没把能源限定传下去」这个变异盖不住 ⇒ 修复形同虚设。
    """
    import asyncio

    from app.agent.engine import AgentEngine
    from app.agent.session import SessionStore
    from app.common.llm import LLMClient

    _seed_two_energies(db_session)
    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())
    sid = engine._store.create()

    out = asyncio.run(engine.respond(db_session, sid, "能源限定车系的纯电版有哪些款型"))

    assert "增程版" not in (out.explanation or ""), out.explanation
    assert "纯电版" in (out.explanation or ""), out.explanation


def test_no_matching_energy_says_so_instead_of_listing_others(db_session: Session):
    """库里只有增程版、用户问纯电版 ⇒ **如实说没有**，不端增程版上来。"""
    source = make_source(db_session, name="无匹配能源来源")
    brand = make_brand(db_session, name="无匹配能源品牌", source=source)
    series = make_series(
        db_session, brand, name="只有增程的车系", body_type="sedan",
        energy_types=("EREV",), source=source,
    )
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="增程版", energy_type="EREV",
        price_cny="160000", source=source,
    )
    db_session.commit()

    text, rows = build_variant_diff_answer(
        db_session, series, brand, "有哪些款型", energy_preference=["BEV"]
    )

    assert not rows, "不该把增程版端上来"
    assert "没有纯电版" in text, text
