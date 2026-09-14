"""SKU 抓取工具的两处修复（2026-09-14 事故）：

1. `--ids` 显式指定时不再受 A-Z 索引范围限制（索引会漏车系：实测「凯美瑞」不在索引里，
   但接口正常返回 13 款型），元数据回落数据库；
2. 抓到 0 款型**不得**记为 sku_done（否则该车系被永久跳过，缺口永不收敛）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tools"))

import fetch_autohome_sku as tool  # noqa: E402

from app.common.models import ExternalSeriesRef  # noqa: E402
from tests.seed import make_brand, make_series, make_source  # noqa: E402


def _seed(db: Session, external_id: str = "110", source_name: str = "汽车之家") -> int:
    source = make_source(db, name=source_name)
    brand = make_brand(db, name="丰田", brand_type="japanese", source=source)
    series = make_series(db, brand, name="凯美瑞", source=source)
    series.price_range_note = "17.18-26.98万元"
    db.add(ExternalSeriesRef(source_id=source.id, external_id=external_id, series_id=series.id))
    db.commit()
    return series.id


def test_explicit_scope_uses_database_metadata(db_session: Session):
    """不在索引里的 id：从库内补齐品牌/车系/价格区间，供抓取与导入使用。"""
    _seed(db_session)
    entries = tool._explicit_scope(["110"], session=db_session)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["external_id"] == "110"
    assert entry["name"] == "凯美瑞"
    assert entry["brand"] == "丰田"
    assert entry["price_note"] == "17.18-26.98万元"
    assert entry["brand_type"]  # 供 build_sku_payload 使用


def test_explicit_scope_ignores_other_sources(db_session: Session):
    """映射按来源限定：别的来源用了同一个 external_id 时不得被当成汽车之家车系。

    `external_series_refs` 的唯一键是 (source_id, external_id)，不同来源可能重号。
    """
    _seed(db_session, external_id="110", source_name="其他来源")
    assert tool._explicit_scope(["110"], session=db_session) == []


def test_explicit_scope_skips_unknown_ids(db_session: Session):
    _seed(db_session)
    assert tool._explicit_scope(["999999"], session=db_session) == []


class _Args:
    def __init__(self, **kw):
        self.ids = kw.get("ids")
        self.limit = kw.get("limit", 0)
        self.no_import = True
        self.save_raw = False


def test_stage_sku_includes_ids_outside_index(monkeypatch, db_session: Session, tmp_path: Path):
    """核心回归：索引里没有该车系时，`--ids 110` 仍必须去抓（此前会「待抓取 0 个」）。"""
    _seed(db_session)
    scope_path = tmp_path / "scope.json"
    scope_path.write_text("[]", encoding="utf-8")  # 索引为空（或漏了该车系）
    monkeypatch.setattr(tool, "SCOPE_PATH", str(scope_path))
    monkeypatch.setattr(tool, "CHECKPOINT_PATH", str(tmp_path / "cp.json"))
    monkeypatch.setattr(tool, "_load_checkpoint", lambda: {"sku_done": {}, "failures": {}, "compliance": {}})
    saved: dict = {}
    monkeypatch.setattr(tool, "_save_checkpoint", lambda cp: saved.update(cp))
    monkeypatch.setattr(tool, "_record_compliance", lambda *a, **k: None)
    monkeypatch.setattr(tool, "fetch_robots", lambda url: None)
    monkeypatch.setattr(tool, "SLEEP_SECONDS", 0)
    # 注入测试库会话（先取原函数再替换，避免自递归）
    original_scope = tool._explicit_scope
    monkeypatch.setattr(tool, "_explicit_scope", lambda ids, session=None: original_scope(ids, session=db_session))

    fetched: list[str] = []

    def fake_fetch(sid: str) -> dict:
        fetched.append(sid)
        return {"variants": [{"name": "2025款 2.0G"}]}

    monkeypatch.setattr(tool, "_fetch_sku_with_retry", fake_fetch)
    monkeypatch.setattr(tool, "build_sku_payload", lambda brand, series, parsed, url: {
        "series": [{"model_years": [{"variants": [{"name": "1"}]}]}],
        "brands": [],
    })

    assert tool.stage_sku(_Args(ids="110")) == 0
    assert fetched == ["110"], "索引外的显式 id 也必须被抓取"
    assert saved["sku_done"]["110"] == "ok"


def test_zero_variants_is_recorded_as_failure(monkeypatch, db_session: Session, tmp_path: Path):
    """抓到 0 款型不得记为完成（否则永久跳过、缺口不收敛）。"""
    _seed(db_session)
    monkeypatch.setattr(tool, "SCOPE_PATH", str(tmp_path / "scope.json"))
    (tmp_path / "scope.json").write_text("[]", encoding="utf-8")
    cp = {"sku_done": {}, "failures": {}, "compliance": {}}
    monkeypatch.setattr(tool, "_load_checkpoint", lambda: cp)
    monkeypatch.setattr(tool, "_save_checkpoint", lambda data: None)
    monkeypatch.setattr(tool, "_record_compliance", lambda *a, **k: None)
    monkeypatch.setattr(tool, "fetch_robots", lambda url: None)
    monkeypatch.setattr(tool, "SLEEP_SECONDS", 0)
    original_scope = tool._explicit_scope
    monkeypatch.setattr(tool, "_explicit_scope", lambda ids, session=None: original_scope(ids, session=db_session))
    monkeypatch.setattr(tool, "_fetch_sku_with_retry", lambda sid: {"variants": []})
    monkeypatch.setattr(tool, "build_sku_payload", lambda *a, **k: {"series": [{"model_years": []}], "brands": []})

    tool.stage_sku(_Args(ids="110"))
    assert "110" not in cp["sku_done"], "0 款型不得记为完成"
    assert "110" in cp["failures"], "应记为失败以便下次重试"
