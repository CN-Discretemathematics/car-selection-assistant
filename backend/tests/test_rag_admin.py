"""管理后台 RAG 管理接口测试（§16.6：可视化 / 试运行 / 索引重建 / 评测报告）。"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.rag import service as rag
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

ADMIN = {"Authorization": "Bearer test-admin-token"}


def _seed(db: Session) -> None:
    source = make_source(db, name="官方测试来源")
    brand = make_brand(db, name="测试品牌", source=source)
    suv = make_series(db, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db, suv)
    make_variant(db, suv, year, config_version="标准版", energy_type="BEV", price_cny="129800",
                 facts=[("座位数", "座位数(个)", "5", "座", None)], source=source)
    db.commit()


def test_rag_admin_requires_auth(client: TestClient):
    assert client.get("/api/v1/admin/rag/status").status_code == 401
    assert client.get("/api/v1/admin/rag/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_rag_graph_endpoint(client: TestClient):
    body = client.get("/api/v1/admin/rag/graph", headers=ADMIN).json()
    assert {"query", "ingest"} <= body.keys()
    assert any(n["id"] == "fuse" for n in body["query"]["nodes"])
    assert "flowchart" in body["query"]["mermaid"]


def test_rag_status_endpoint(client: TestClient, db_session: Session):
    _seed(db_session)
    rag.reset_index()
    body = client.get("/api/v1/admin/rag/status", headers=ADMIN).json()
    assert body["backend_mode"] == "inmemory"
    assert body["strategy"]["chunk_size"] > 0
    assert body["db_counts"]["series"] == 1


def test_rag_status_exposes_dense_watermark(client: TestClient, db_session: Session, monkeypatch):
    """水位字段必须穿过 Pydantic 响应模型（评审 v5-1：schema 漏字段会让整个滞后判定静默消失）。"""
    _seed(db_session)
    rag.reset_index()
    work = Path(__file__).resolve().parent / ".tmp" / "watermark-status"
    (work / ".tmp").mkdir(parents=True, exist_ok=True)
    (work / ".tmp" / "dense-build-meta.json").write_text(
        '{"built_at": "2026-09-08T13:14:00+00:00", "chunks": 12078, '
        '"sales_month": "2026-07", "indexed": 12078}',
        encoding="utf-8",
    )
    monkeypatch.chdir(work)

    dense = client.get("/api/v1/admin/rag/status", headers=ADMIN).json()["dense"]
    assert dense["built_at"] == "2026-09-08T13:14:00+00:00"
    assert dense["chunks"] == 12078
    assert dense["sales_month"] == 202607
    assert dense["db_sales_month"] is None  # 测试库无销量数据
    assert dense["stale"] is False


def test_rag_try_query_and_runs(client: TestClient, db_session: Session):
    _seed(db_session)
    rag.reset_index()
    resp = client.post("/api/v1/admin/rag/query", headers=ADMIN,
                       json={"query": "家用SUV 座位数", "top_k": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run"]["stages"][0]["node"] == "analyze"
    assert body["results"]

    runs = client.get("/api/v1/admin/rag/runs", headers=ADMIN).json()["runs"]
    assert runs and runs[0]["query"] == "家用SUV 座位数"


def test_rag_try_query_rejects_unknown_filters(client: TestClient, db_session: Session):
    _seed(db_session)
    rag.reset_index()
    resp = client.post("/api/v1/admin/rag/query", headers=ADMIN,
                       json={"query": "家用SUV", "filters": {"drop_table": 1, "series_id": 1}})
    assert resp.status_code == 200
    # 非白名单过滤键被丢弃，不会进入检索层
    assert resp.json()["run"]["filters"] == {"series_id": 1}


def test_rag_reindex_sparse(client: TestClient, db_session: Session):
    _seed(db_session)
    rag.reset_index()
    body = client.post("/api/v1/admin/rag/reindex", headers=ADMIN, json={"target": "sparse"}).json()
    assert body["mode"] == "sync"
    assert body["summary"]["indexed"] > 0
    assert body["summary"]["by_kind"]["series_intro"] == 1


def test_rag_reindex_invalid_target(client: TestClient):
    resp = client.post("/api/v1/admin/rag/reindex", headers=ADMIN, json={"target": "everything"})
    assert resp.status_code == 422


def test_rag_eval_report_missing(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.admin.rag_router._EVAL_DIR", Path("nonexistent-eval-dir"))
    resp = client.get("/api/v1/admin/rag/eval", headers=ADMIN)
    assert resp.status_code == 400


def test_rag_eval_report_present(client: TestClient, monkeypatch):
    import json

    fake_dir = Path("tests/.tmp/eval_fixture")
    fake_dir.mkdir(parents=True, exist_ok=True)
    (fake_dir / "rag_eval_report.json").write_text(
        json.dumps({"strategies": [{"strategy": "sparse", "@5": {"hit": 0.8}}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.admin.rag_router._EVAL_DIR", fake_dir)
    body = client.get("/api/v1/admin/rag/eval", headers=ADMIN).json()
    assert body["rag"]["strategies"][0]["strategy"] == "sparse"
