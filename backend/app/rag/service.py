"""RAG 门面：唯一对外入口（Agent 工具 / 索引工具 / 管理后台共用）。

- search()：跑查询流水线（app/rag/pipeline.py），签名与旧 retrieval.service 兼容；
- try_query()：带完整阶段轨迹的调试运行（管理后台「试运行」）；
- run_reindex()：跑摄取流水线重建索引（sparse=进程内 BM25 / dense=Zilliz）；
- get_status() / recent_runs() / graph_spec()：管理后台可视化数据源。

索引新鲜度策略：
- RETRIEVAL_BACKEND=inmemory（开发/测试）：按数据量快照自动重建稀疏索引（重建廉价）；
- RETRIEVAL_BACKEND=milvus（生产）：进程首次使用时构建一次稀疏索引，稠密索引由
  tools/build_retrieval_index.py 或管理后台显式重建（全量 embedding 代价高）。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.models import MonthlySales, SourceDocument, SpecFact, VehicleSeries, VehicleVariant
from app.rag.ingest import get_ingest_graph, run_ingest
from app.rag.pipeline import build_query_graph
from app.rag.rerank import get_reranker
from app.retrieval.backends import InMemoryRetriever, RetrievalBackend, SearchResult
from app.retrieval.config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    HYDE_ENABLED,
    MAX_CHUNKS,
    MILVUS_COLLECTION,
    MILVUS_DIM,
    MILVUS_TOKEN,
    MILVUS_URI,
    RAG_RUN_LOG_SIZE,
    RECALL_MULTIPLIER,
    RERANK_MODEL,
    RERANK_PROVIDER,
    RETRIEVAL_BACKEND,
    RRF_K,
    RRF_WEIGHT_DENSE,
    RRF_WEIGHT_SPARSE,
    TOKENIZER,
)

_lock = threading.Lock()

_sparse_backend: InMemoryRetriever | None = None
_dense_backend: RetrievalBackend | None = None
_dense_probe_error: str | None = None  # milvus 配置不完整时的原因（状态页展示）
_indexed_counts: tuple[int, int, int, int] | None = None  # (documents, facts, series, variants)
_sparse_built_at: float | None = None
_run_log: deque[dict] = deque(maxlen=max(RAG_RUN_LOG_SIZE, 1))
_query_graph = None


# ── 后端与索引 ────────────────────────────────────────────────────────────────
def get_sparse_backend() -> InMemoryRetriever:
    global _sparse_backend
    if _sparse_backend is None:
        _sparse_backend = InMemoryRetriever()
    return _sparse_backend


def get_dense_backend() -> RetrievalBackend | None:
    """稠密召回后端：仅 RETRIEVAL_BACKEND=milvus 且 URI/Token 齐备时可用，否则 None。"""
    global _dense_backend, _dense_probe_error
    if RETRIEVAL_BACKEND != "milvus":
        return None
    if _dense_backend is None and _dense_probe_error is None:
        if MILVUS_URI and MILVUS_TOKEN:
            from app.retrieval.zilliz import ZillizRestRetriever

            _dense_backend = ZillizRestRetriever()
        else:
            _dense_probe_error = "RETRIEVAL_BACKEND=milvus 但 MILVUS_URI/MILVUS_TOKEN 未配置"
    return _dense_backend


def _counts(db: Session) -> tuple[int, int, int, int]:
    """索引快照：来源文档/事实/系列/SKU 数量，任一变化触发开发模式自动重建。"""
    docs = db.scalar(select(func.count()).select_from(SourceDocument)) or 0
    facts = db.scalar(select(func.count()).select_from(SpecFact)) or 0
    series = db.scalar(select(func.count()).select_from(VehicleSeries)) or 0
    variants = db.scalar(select(func.count()).select_from(VehicleVariant)) or 0
    return docs, facts, series, variants


def ensure_sparse_index(db: Session, reindex: bool = False) -> None:
    """保证稀疏（BM25）索引可用：开发模式按数据量快照自动重建；生产首用构建一次。"""
    global _indexed_counts, _sparse_built_at
    auto = RETRIEVAL_BACKEND != "milvus"
    with _lock:
        counts = _counts(db) if auto else None
        needs = reindex or _sparse_built_at is None or (auto and _indexed_counts != counts)
        if not needs:
            return
        run_ingest(db, target="sparse")
        _indexed_counts = counts
        _sparse_built_at = time.time()


def run_reindex(
    db: Session,
    target: str = "sparse",
    limit: int = MAX_CHUNKS,
    on_progress: Any = None,
) -> dict:
    """重建索引（管理后台/索引工具入口），返回摄取流水线运行摘要。

    sparse 重建持锁串行（避免并发双写 BM25 索引）；dense 灌库耗时长，不持锁。
    """
    global _indexed_counts, _sparse_built_at
    if target != "sparse":
        return run_ingest(db, target=target, limit=limit, on_progress=on_progress)
    with _lock:
        summary = run_ingest(db, target=target, limit=limit, on_progress=on_progress)
        _indexed_counts = _counts(db) if RETRIEVAL_BACKEND != "milvus" else None
        _sparse_built_at = time.time()
    return summary


def reset_index() -> None:
    """测试辅助：清空索引快照与后端单例。"""
    global _sparse_backend, _dense_backend, _dense_probe_error, _indexed_counts, _sparse_built_at
    with _lock:
        _sparse_backend = None
        _dense_backend = None
        _dense_probe_error = None
        _indexed_counts = None
        _sparse_built_at = None


# ── 查询流水线 ────────────────────────────────────────────────────────────────
def get_query_graph():
    global _query_graph
    if _query_graph is None:
        _query_graph = build_query_graph()
    return _query_graph


def _invoke(db: Session, query: str, filters: dict | None, top_k: int) -> dict:
    ensure_sparse_index(db)
    started = time.perf_counter()
    state = get_query_graph().invoke(
        {"db": db, "query": query, "filters": filters or {}, "top_k": top_k, "stages": [], "warnings": []}
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query": query[:120],
        "filters": filters or {},
        "top_k": top_k,
        "duration_ms": duration_ms,
        "result_count": len(state.get("results") or []),
        "resolved_series": state.get("resolved_series") or [],
        "query_type": state.get("query_type") or "",
        "stages": state.get("stages") or [],
        "warnings": state.get("warnings") or [],
        "dense": get_dense_backend() is not None,
    }
    _run_log.append(record)
    return {"state": state, "record": record}


def search(
    db: Session,
    query: str,
    filters: dict[str, Any] | None = None,
    top_k: int = 5,
    reindex: bool = False,
) -> list[SearchResult]:
    """统一搜索入口（Agent 工具 retrieval_search 的实现）。

    analyze → sparse ∥ dense 召回 → RRF 融合 → 重排 → 证据把关；
    reindex=True 时强制重建稀疏索引后再查询。
    """
    if reindex:
        run_reindex(db, target="sparse")
    return _invoke(db, query, filters, top_k)["state"].get("results") or []


def try_query(db: Session, query: str, filters: dict | None = None, top_k: int = 5) -> dict:
    """调试运行：返回完整阶段轨迹 + 结果（管理后台「试运行」瀑布图数据源）。"""
    out = _invoke(db, query, filters, top_k)
    state = out["state"]
    return {
        "run": out["record"],
        "results": [_result_dict(r) for r in state.get("results") or []],
    }


def _result_dict(r: SearchResult) -> dict:
    return {
        "chunk_id": r.chunk_id,
        "score": r.score,
        "text": r.text,
        "kind": r.kind,
        "brand_id": r.brand_id,
        "series_id": r.series_id,
        "variant_id": r.variant_id,
        "source_id": r.source_id,
        "source_url": r.source_url,
    }


def recent_runs(limit: int = 20) -> list[dict]:
    """最近的流水线运行轨迹（新→旧）。"""
    runs = list(_run_log)
    runs.reverse()
    return runs[: max(limit, 1)]


# ── 可视化与状态 ──────────────────────────────────────────────────────────────
def _graph_spec(compiled: Any) -> dict:
    drawable = compiled.get_graph()
    nodes = [{"id": n} for n in drawable.nodes]
    edges = [
        {"source": e.source, "target": e.target, "conditional": bool(e.conditional)}
        for e in drawable.edges
    ]
    return {"nodes": nodes, "edges": edges, "mermaid": drawable.draw_mermaid()}


def graph_spec() -> dict:
    """两条流水线的结构（节点/边/mermaid 源码），供管理后台绘制流程图。"""
    return {
        "query": _graph_spec(get_query_graph()),
        "ingest": _graph_spec(get_ingest_graph()),
    }


def get_status(db: Session | None = None) -> dict:
    """RAG 运行状态：后端/索引/配置摘要（不含任何密钥明文）。"""
    sparse = get_sparse_backend()
    by_kind: dict[str, int] = {}
    for chunk in sparse.chunks:
        by_kind[chunk.kind] = by_kind.get(chunk.kind, 0) + 1
    dense = get_dense_backend()
    reranker = get_reranker()
    status: dict[str, Any] = {
        "backend_mode": RETRIEVAL_BACKEND,
        "sparse": {
            "backend": "bm25",
            "chunks": len(sparse.chunks),
            "by_kind": by_kind,
            "built_at": (
                datetime.fromtimestamp(_sparse_built_at, tz=timezone.utc).isoformat(timespec="seconds")
                if _sparse_built_at
                else None
            ),
        },
        "dense": {
            "enabled": dense is not None,
            "backend": getattr(dense, "name", None),
            "collection": MILVUS_COLLECTION if dense is not None else None,
            "dim": MILVUS_DIM if dense is not None else None,
            "embedding_model": EMBEDDING_MODEL if EMBEDDING_BASE_URL else None,
            "error": _dense_probe_error,
            # 水位（2026-09 新增）：dense-build-meta.json 由重建工具写入
            # （.tmp 目录随 compose volume 持久化）；latest_sales_month 为数据库水位——
            # 两者对比即可判断集合是否滞后于最新销量数据
            **dense_build_meta(),
        },
        "reranker": {
            "provider": RERANK_PROVIDER or "lexical",
            "active": reranker.name,
            "model": RERANK_MODEL if reranker.name == "cross_encoder" else None,
        },
        "strategy": {
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
            "tokenizer": TOKENIZER,
            "recall_multiplier": RECALL_MULTIPLIER,
            "rrf_k": RRF_K,
            "rrf_weights": [RRF_WEIGHT_SPARSE, RRF_WEIGHT_DENSE],
            "hyde": HYDE_ENABLED,
            "max_chunks": MAX_CHUNKS,
        },
        "runs_logged": len(_run_log),
    }
    if db is not None:
        status["db_counts"] = dict(zip(("documents", "facts", "series", "variants"), _counts(db)))
        latest = db.scalar(select(func.max(MonthlySales.month)))
        # 销量月份转 YYYYMM 整数（2026-08 → 202608），与 RagStatusOut.db_counts 的 int 字段一致
        status["db_counts"]["latest_sales_month"] = _as_yyyymm(latest)
    # 无 db 时也补齐字段（db_sales_month=None）：消费者按「字段恒在」判断，避免缺字段被当成新鲜
    annotate_dense_watermark(status)
    return status


def _as_yyyymm(value: Any) -> int | None:
    """销量月份归一化为 YYYYMM 整数（'2026-08' / '2026-8' / '202608' / 202608 → 202608）。"""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 190001 <= value <= 999912 else None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) == 5:  # 库里出现未补零的 '2026-8'
        return int(digits[:4]) * 100 + int(digits[4])
    return int(digits[:6]) if len(digits) >= 6 else None


def annotate_dense_watermark(status: dict) -> None:
    """给 status['dense'] 补水位对比字段：集合构建时的销量月份 vs 库内最新月份。

    stale=True 表示「集合可能滞后于库内数据」，三种情形都算：
    ① 标记里的销量月份 < 库内最新月份（确实落后一个月）；
    ② 标记文件缺失（旧版工具灌入或重建从未成功过）——无法证明新鲜，按需重建暴露；
    ③ 标记有 built_at 但没写下销量月份（构建时那次查询失败）——同样无法证明新鲜。
    只有 built_at 与 sales_month 都齐、且不落后时才是 stale=False。
    """
    dense = status.get("dense")
    db_month = _as_yyyymm((status.get("db_counts") or {}).get("latest_sales_month"))
    if not isinstance(dense, dict):
        return
    dense["db_sales_month"] = db_month
    if not dense.get("built_at"):
        dense["sales_month"] = None
        dense["stale"] = db_month is not None
        dense["stale_reason"] = (
            "缺少构建水位标记 dense-build-meta.json，无法确认集合是否与库内数据同步"
            if db_month is not None
            else None
        )
        return
    month = _as_yyyymm(dense.get("sales_month"))
    dense["sales_month"] = month
    if month is None:
        # 标记里没写销量月份（构建时该查询异常被吞掉）：不能当成「新鲜」
        dense["stale"] = db_month is not None
        dense["stale_reason"] = (
            "构建水位标记缺少销量月份，无法确认集合是否与库内数据同步" if db_month is not None else None
        )
        return
    dense["stale"] = bool(db_month and month < db_month)
    dense["stale_reason"] = (
        f"集合构建于销量 {month}，库内已到 {db_month}，需重建稠密索引" if dense["stale"] else None
    )


def dense_build_meta() -> dict:
    """稠密集合构建水位：读重建工具写下的 dense-build-meta.json。"""
    try:
        p = Path(".tmp") / "dense-build-meta.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            "built_at": data.get("built_at"),
            "chunks": data.get("indexed") or data.get("chunks"),
            # 归一化为 YYYYMM 整数：响应模型该字段是 int，字符串会让 /ops/rag 直接 500
            "sales_month": _as_yyyymm(data.get("sales_month")),
        }
    except (OSError, ValueError, AttributeError):
        # 文件缺失/半写/坏 JSON 时水位降级；刻意不吞 NameError 之类的编码错误
        # （曾因本模块漏 import Path 让水位一直静默返回空，见 2026-09-10 排查）
        return {}
