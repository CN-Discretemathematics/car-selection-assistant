"""第 4 批：回答声明覆盖范围 / scan_secrets 前导下划线 / AGENTS.md 基线数。"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.agent.series_qa import build_series_qa_answer
from app.catalog.series_index import active_series_count, resolve_series
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

# 与 `reviewer/scan_secrets.py` 里那条同名规则保持一致（此处独立复制一份，
# 免得测门禁时反过来依赖门禁自己的实现）
_SNAKE_CASE = re.compile(r"[a-z_][a-z0-9]*(?:_[a-z0-9]+)+")


def _seed(db: Session) -> None:
    src = make_source(db, name="汽车之家")
    vw = make_brand(db, name="大众", source=src)
    for name in ("朗逸", "轩逸"):
        s = make_series(db, vw, name=name, source=src, positioning="紧凑型车")
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db.commit()


# ── ① 回答里声明覆盖范围 ────────────────────────────────────────────────────


def test_answer_states_the_library_scope(db_session: Session) -> None:
    """回答里必须说清「只覆盖库内在售的 N 个车系」。

    此前用户问「大众朗逸和明锐哪个好」只得到朗逸的回答、**没有任何提示**说明锐
    库里没有，会以为看全了。说出范围比逐个点名「哪个没有」诚实且零维护成本——
    要说准某台车缺，前提是能认出它是个车型名，而库里没有它就需要一份全量车型名录。
    """
    _seed(db_session)
    msg = "大众朗逸和轩逸哪个好"
    resolved = resolve_series(db_session, msg)
    text = build_series_qa_answer(db_session, resolved, msg)
    assert "库内在售车系" in text, f"回答应声明覆盖范围。实际末尾：{text[-200:]}"
    n = active_series_count(db_session)
    assert f"{n} 个" in text, f"范围里应带上在售车系数 {n}。实际末尾：{text[-200:]}"
    assert "未收录的车型不在此列" in text


def test_active_series_count_matches_db(db_session: Session) -> None:
    """声明的那个数必须与库里真实的在售车系数一致——说错数比不说更糟。"""
    _seed(db_session)
    from sqlalchemy import func, select

    from app.common.models import VehicleSeries

    real = db_session.execute(
        select(func.count(VehicleSeries.id))
        .where(VehicleSeries.active_status == "active")
    ).scalar()
    assert active_series_count(db_session) == real


# ── ② scan_secrets 漏报前导下划线标识符 ─────────────────────────────────────


def test_scan_secrets_matches_leading_underscore_identifiers() -> None:
    """`_private_key` 这类**私有名**此前一个都扫不出来。

    原正则 `[a-z][a-z0-9]*(?:_[a-z0-9]+)+` **要求以字母开头**，于是前导下划线
    直接出局——而 Python 私有属性/私有常量正是写成 `_name`。
    """
    for name in ("_private_key", "_db_password", "_api_key_secret"):
        assert _SNAKE_CASE.fullmatch(name), f"「{name}」应被判为 snake_case 私有名"


def test_scan_secrets_did_not_add_noise() -> None:
    """覆盖面扩了，**噪音面不能跟着扩**。"""
    for name in ("self", "x", "a", "class", "def", "ABC_def", "Mixed_Case",
                 "__init__", "3d_render"):
        assert not _SNAKE_CASE.fullmatch(name), f"「{name}」不该被判为疑似密钥"


# ── ③ AGENTS.md 的基线数与实测一致 ──────────────────────────────────────────


def test_agentsmd_ruff_baseline_matches_reality() -> None:
    """`AGENTS.md` 写「基线 130 条」，而实测长期是 **129**（连续五轮）。

    文档与现实不符会直接误导下一个人——「基线 130」意味着多出来的 1 条是新增回归，
    而实际上它是文档写错了。2026-10-07 用户拍板改为 129。
    """
    from pathlib import Path

    agents = (Path(__file__).resolve().parents[2] / "AGENTS.md").read_text(encoding="utf-8")
    assert "基线 **130 条**" not in agents, "AGENTS.md 仍写着过期的 130 条基线"
    assert "基线 **129 条**" in agents, "AGENTS.md 应写 129 条基线"
