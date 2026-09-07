"""RAG 流程管理接口（流程可视化 + 运维管理）。

- GET  /admin/rag/graph             两条 LangGraph 流水线结构（节点/边/mermaid 源码）
- GET  /admin/rag/status            后端/索引/策略配置摘要（不含密钥）
- GET  /admin/rag/runs              最近查询流水线运行轨迹（阶段瀑布数据）
- POST /admin/rag/query             试运行：单条查询的完整阶段轨迹 + 命中结果
- POST /admin/rag/reindex           重建索引（sparse 同步；dense 后台任务 + 进度）
- GET  /admin/rag/reindex-progress  dense 重建进度
- GET  /admin/rag/eval              评测报告（eval/rag_eval_report.json、eval/report.json）

全部走管理后台独立凭据（require_admin），前端可视化页面见 web /ops/rag。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.admin.rag_schemas import (
    RagEvalOut,
    RagGraphOut,
    RagResultOut,
    RagStatusOut,
    ReindexOut,
    ReindexProgressOut,
    ReindexSummaryOut,
    RunRecordOut,
    RunsOut,
    TryQueryOut,
)
from app.admin.router import require_admin
from app.common.database import get_session, get_session_factory
from app.common.errors import bad_request
from app.rag import service as rag

router = APIRouter(tags=["admin-rag"])

_ALLOWED_FILTER_KEYS = {"brand_id", "series_id", "model_year_id", "variant_id", "source_id", "energy_type"}
_EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"

# dense 重建进度（进程内单例：同一时间只允许一个重建任务）
_reindex_lock = threading.Lock()
_reindex_state: dict[str, Any] = {
    "running": False,
    "target": None,
    "progress": None,  # {"done": int, "total": int}
    "summary": None,
    "error": None,
}


class RagQueryIn(BaseModel):
    query: str = Field(max_length=500)
    filters: dict[str, Any] | None = None
    top_k: int = Field(default=5, ge=1, le=10)


class RagReindexIn(BaseModel):
    target: str = Field(default="sparse", pattern="^(sparse|dense)$")


@router.get("/admin/rag/graph", response_model=RagGraphOut, dependencies=[Depends(require_admin)])
def rag_graph() -> RagGraphOut:
    return RagGraphOut(**rag.graph_spec())


@router.get("/admin/rag/status", response_model=RagStatusOut, dependencies=[Depends(require_admin)])
def rag_status(db: Session = Depends(get_session)) -> RagStatusOut:
    return RagStatusOut(**rag.get_status(db))


@router.get("/admin/rag/runs", response_model=RunsOut, dependencies=[Depends(require_admin)])
def rag_runs(limit: int = 20) -> RunsOut:
    return RunsOut(runs=[RunRecordOut(**r) for r in rag.recent_runs(min(max(limit, 1), 50))])


@router.post("/admin/rag/query", response_model=TryQueryOut, dependencies=[Depends(require_admin)])
def rag_try_query(payload: RagQueryIn, db: Session = Depends(get_session)) -> TryQueryOut:
    safe_filters = {
        k: v for k, v in (payload.filters or {}).items() if k in _ALLOWED_FILTER_KEYS
    }
    out = rag.try_query(db, payload.query, safe_filters or None, payload.top_k)
    return TryQueryOut(
        run=RunRecordOut(**out["run"]),
        results=[RagResultOut(**r) for r in out["results"]],
    )


def _run_dense_reindex(limit: int) -> None:
    """后台任务：dense 灌库（独立 DB 会话；进度写进程内状态）。"""
    def on_progress(done: int, total: int) -> None:
        _reindex_state["progress"] = {"done": done, "total": total}

    try:
        factory = get_session_factory()
        with factory() as db:
            summary = rag.run_reindex(db, target="dense", limit=limit, on_progress=on_progress)
        _reindex_state["summary"] = {
            k: summary[k] for k in ("target", "chunks", "indexed", "by_kind", "warnings")
        }
    except Exception as err:  # noqa: BLE001 - 后台任务异常记录到进度状态
        _reindex_state["error"] = f"{type(err).__name__}: {str(err)[:300]}"
    finally:
        _reindex_state["running"] = False


@router.post("/admin/rag/reindex", response_model=ReindexOut, dependencies=[Depends(require_admin)])
def rag_reindex(
    payload: RagReindexIn,
    background: BackgroundTasks,
    limit: int = 0,
    db: Session = Depends(get_session),
) -> ReindexOut:
    effective_limit = limit if limit > 0 else None
    if payload.target == "sparse":
        summary = rag.run_reindex(db, target="sparse", limit=effective_limit or 20000)
        return ReindexOut(mode="sync", summary=ReindexSummaryOut(**summary))

    with _reindex_lock:
        if _reindex_state["running"]:
            raise HTTPException(status_code=409, detail="已有 dense 重建任务在进行中。")
        _reindex_state.update(
            {"running": True, "target": "dense", "progress": None, "summary": None, "error": None}
        )
    background.add_task(_run_dense_reindex, effective_limit or 20000)
    return ReindexOut(mode="background", summary=None)


@router.get("/admin/rag/reindex-progress", response_model=ReindexProgressOut,
            dependencies=[Depends(require_admin)])
def rag_reindex_progress() -> ReindexProgressOut:
    return ReindexProgressOut(**dict(_reindex_state))


@router.get("/admin/rag/eval", response_model=RagEvalOut, dependencies=[Depends(require_admin)])
def rag_eval() -> RagEvalOut:
    """读取评测报告文件（tools/eval_rag.py 与 tools/eval_agent.py 的产物）。"""
    out: dict[str, Any] = {}
    for key, filename in (("rag", "rag_eval_report.json"), ("agent", "report.json")):
        path = _EVAL_DIR / filename
        if path.exists():
            try:
                out[key] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as err:
                out[key] = {"error": f"报告解析失败：{err}"}
        else:
            out[key] = None
    if not any(out.values()):
        raise bad_request("暂无评测报告：先运行 python tools/eval_rag.py 生成。")
    return RagEvalOut(**out)
