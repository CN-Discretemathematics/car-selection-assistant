"""查询流水线（LangGraph StateGraph）：主流两阶段混合检索编排。

    START → analyze → ┬ recall_sparse(BM25)      ┬ → fuse(RRF) → rerank → grade → END
                      ┴ recall_dense(向量, 可选)  ┘

- analyze：查询理解——解析消息中的真实车系（名称索引），生成实体增强查询与
  元数据过滤条件（单车系自动加 series_id 过滤，抑制「语义近但实体错」的噪声）；
- recall_sparse / recall_dense：并行多路召回（每路 top_k × RECALL_MULTIPLIER）；
  dense 未配置或调用失败时自动降级为纯稀疏（原则 7），warning 记入运行轨迹；
- fuse：RRF（k=60）融合多路名次，跨后端按 chunk_id 去重、按文本兜底去重；
- rerank：Cross-Encoder API（配置时）或词元重叠 lexical（默认）精排到 top_k；
- grade：证据把关（corrective-RAG 思路）——绝对相关分低于阈值/空文本的证据丢弃。

节点为确定性同步函数、只返回自身增量（并行分支写同键会触发 InvalidUpdateError）；
stages/warnings 走 operator.add reducer。异步调用方经 run_in_threadpool 执行。
"""
from __future__ import annotations

import time

from langgraph.graph import END, START, StateGraph

from app.catalog.series_index import display_name, resolve_series
from app.rag.rerank import LexicalReranker, get_reranker, rrf_fuse
from app.rag.state import RagState, make_stage
from app.retrieval.backends import SearchResult, tokenize
from app.retrieval.config import RECALL_MULTIPLIER, RELEVANCE_THRESHOLD, RRF_K

_RECALL_FLOOR = 20
_RECALL_CEILING = 100


def _analyze(state: RagState) -> RagState:
    """查询理解：车系实体解析 → 增强查询 + 元数据过滤。"""
    started = time.perf_counter()
    query = (state.get("query") or "").strip()
    filters = dict(state.get("filters") or {})
    top_k = min(max(int(state.get("top_k") or 5), 1), 10)
    recall_k = min(max(top_k * max(RECALL_MULTIPLIER, 1), _RECALL_FLOOR), _RECALL_CEILING)

    resolved_names: list[str] = []
    warnings: list[str] = []
    if query:
        try:
            resolved = resolve_series(state["db"], query)
        except Exception as err:  # noqa: BLE001 - 实体解析失败不阻断检索
            resolved = []
            warnings.append(f"车系解析失败（按无实体处理）：{err}")
        resolved_names = [display_name(s, b) for s, b in resolved]
        if len(resolved) == 1 and "series_id" not in filters:
            filters["series_id"] = resolved[0][0].id

    # 实体增强：把解析出的规范车系名并入查询（别名/口语 → 索引用语），已含则不重复
    entity_query = query
    extra = [n for n in resolved_names if n and n.lower() not in query.lower()]
    if extra:
        entity_query = f"{query} {' '.join(extra)}".strip()

    return {
        "query": query,
        "filters": filters,
        "top_k": top_k,
        "recall_k": recall_k,
        "entity_query": entity_query,
        "resolved_series": resolved_names,
        "stages": [make_stage("analyze", 1, len(resolved_names), started,
                              {"resolved_series": resolved_names, "filters": filters, "recall_k": recall_k})],
        "warnings": warnings,
    }


def _route_after_analyze(state: RagState) -> list[str] | str:
    """空查询直接终止；否则并行进入两路召回。"""
    if not state.get("entity_query") or not tokenize(state["entity_query"]):
        return END
    return ["recall_sparse", "recall_dense"]


def _recall_sparse(state: RagState) -> RagState:
    """稀疏召回：进程内 BM25（始终可用）。"""
    from app.rag.service import get_sparse_backend

    started = time.perf_counter()
    backend = get_sparse_backend()
    hits = backend.search(
        state["entity_query"], filters=state.get("filters"), top_k=state["recall_k"]
    )
    return {
        "sparse_hits": hits,
        "stages": [make_stage("recall_sparse", 1, len(hits), started, {"backend": backend.name})],
    }


def _recall_dense(state: RagState) -> RagState:
    """稠密召回：Zilliz/Milvus 向量检索（未配置时空转；失败降级为纯稀疏）。"""
    from app.rag.service import get_dense_backend

    started = time.perf_counter()
    backend = get_dense_backend()
    hits: list[SearchResult] = []
    warnings: list[str] = []
    detail: dict = {"backend": None}
    if backend is not None:
        detail["backend"] = backend.name
        try:
            hits = backend.search(
                state["entity_query"], filters=state.get("filters"), top_k=state["recall_k"]
            )
        except Exception as err:  # noqa: BLE001 - 云端不可用不阻断检索（原则 7）
            warnings.append(f"dense 召回失败（降级为纯稀疏）：{type(err).__name__}: {err}")
            detail["error"] = str(err)[:200]
    return {
        "dense_hits": hits,
        "stages": [make_stage("recall_dense", 1, len(hits), started, detail)],
        "warnings": warnings,
    }


def _fuse(state: RagState) -> RagState:
    """RRF 融合两路召回：名次贡献叠加，chunk_id + 文本两级去重（rrf_fuse 内建）。"""
    started = time.perf_counter()
    sparse = state.get("sparse_hits") or []
    dense = state.get("dense_hits") or []
    deduped = rrf_fuse([sparse, dense])
    return {
        "fused": deduped,
        "stages": [make_stage("fuse", len(sparse) + len(dense), len(deduped), started,
                              {"sparse": len(sparse), "dense": len(dense), "rrf_k": RRF_K})],
    }


def _rerank(state: RagState) -> RagState:
    """精排：Cross-Encoder API（配置时）→ 失败回退 lexical；默认 lexical。"""
    started = time.perf_counter()
    fused = state.get("fused") or []
    top_k = state["top_k"]
    warnings: list[str] = []
    reranker = get_reranker()
    try:
        ranked = reranker.rerank(state["entity_query"], fused, top_k)
    except Exception as err:  # noqa: BLE001 - 重排服务故障不阻断检索
        warnings.append(f"重排失败（回退 lexical）：{type(err).__name__}: {err}")
        reranker = LexicalReranker()
        ranked = reranker.rerank(state["entity_query"], fused, top_k)
    return {
        "reranked": ranked,
        # grade 节点据此决定阈值语义：只有 Cross-Encoder 的绝对相关分可阈值化
        "reranker_absolute": bool(getattr(reranker, "absolute_scores", False)),
        "stages": [make_stage("rerank", len(fused), len(ranked), started, {"reranker": reranker.name})],
        "warnings": warnings,
    }


def _grade(state: RagState) -> RagState:
    """证据把关：丢弃空文本；重排分具备绝对语义时应用相关性阈值。"""
    started = time.perf_counter()
    ranked = state.get("reranked") or []
    absolute = bool(state.get("reranker_absolute"))
    threshold = RELEVANCE_THRESHOLD if absolute else 0.0
    results = [
        hit for hit in ranked
        if hit.text and hit.text.strip() and (threshold <= 0 or hit.score >= threshold)
    ]
    dropped = len(ranked) - len(results)
    final = results[: state["top_k"]]
    return {
        "results": final,
        "stages": [make_stage("grade", len(ranked), len(final), started,
                              {"dropped": dropped, "threshold": threshold if absolute else None})],
    }


def build_query_graph():
    """查询流水线 StateGraph（供 service 编译缓存与管理后台可视化）。"""
    graph = StateGraph(RagState)
    graph.add_node("analyze", _analyze)
    graph.add_node("recall_sparse", _recall_sparse)
    graph.add_node("recall_dense", _recall_dense)
    graph.add_node("fuse", _fuse)
    graph.add_node("rerank", _rerank)
    graph.add_node("grade", _grade)
    graph.add_edge(START, "analyze")
    graph.add_conditional_edges(
        "analyze",
        _route_after_analyze,
        ["recall_sparse", "recall_dense", END],
    )
    graph.add_edge(["recall_sparse", "recall_dense"], "fuse")
    graph.add_edge("fuse", "rerank")
    graph.add_edge("rerank", "grade")
    graph.add_edge("grade", END)
    return graph.compile()
