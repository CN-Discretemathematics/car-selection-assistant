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

import re
import time

from langgraph.graph import END, START, StateGraph

from app.catalog.series_index import display_name, resolve_series
from app.rag.rerank import PassThroughReranker, get_reranker, rrf_fuse
from app.rag.state import RagState, make_stage
from app.rag.synonyms import expand_query
from app.retrieval.backends import SearchResult, tokenize
from app.retrieval.config import (
    HYDE_ENABLED,
    RECALL_MULTIPLIER,
    RELEVANCE_THRESHOLD,
    RRF_K,
    RRF_WEIGHT_DENSE,
    RRF_WEIGHT_SPARSE,
)

_RECALL_FLOOR = 20
_RECALL_CEILING = 100

# 参数事实词（优化④路由启发式）：命中即视为「参数查询」候选
_PARAM_HINT_RE = re.compile(
    r"续航|油耗|耗电|电池|轴距|尺寸|马力|功率|扭矩|座位|几座|指导价|价位|多少钱"
    r"|百公里加速|风阻|油箱|后备厢|行李厢|接近角|离去角|离地间隙|整备质量|轮胎规格"
)
_COMPARE_HINT_RE = re.compile(r"对比|差异|差别|区别|比较|哪个好|比一比|版本差异|款型差异")

# 约束解析（评测规范 v4：recommend 约束下推）——从问题文本解析硬约束，
# grade 据此把「满足约束」的证据排到前面（未点名车系时才生效）
_BUDGET_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万")
_SEATS_RE = re.compile(r"(\d)\s*座")

# 加权 RRF（优化⑤）：统一 0.6/0.4——权重网格消融（120 题五配置，2026-09）实测：
# 0.6/0.4 MRR 0.7032 为最优平台期（0.5/0.5 等权 0.6948、纯稀疏 0.6898、0.4/0.6 有害）；
# 原按桶路由的权重组（semantic 偏稠密）实测反而更差（0.7010），已删除。
# 基准值可在 .env 用 RETRIEVAL_RRF_WEIGHT_* 调整。


def _classify_query(query: str, resolved_count: int) -> str:
    """查询意图启发式分类（确定性，无 LLM 调用）：parameter | compare | semantic。"""
    if _COMPARE_HINT_RE.search(query):
        return "compare"
    if _PARAM_HINT_RE.search(query) and resolved_count >= 1:
        return "parameter"
    return "semantic"


def _parse_constraints(query: str) -> dict:
    """从问题文本解析硬约束（预算/能源/车身/座位），供 grade 约束优先重排。"""
    out: dict = {}
    m = _BUDGET_RE.search(query)
    if m:
        out["budget_max"] = int(float(m.group(1)) * 10000)
    if "纯电" in query:
        out["energy_type"] = "BEV"
    elif "插混" in query or "能加油" in query:
        out["energy_type"] = "PHEV"
    elif "增程" in query:
        out["energy_type"] = "EREV"
    elif "混动" in query:
        out["energy_type"] = "HEV"
    elif "燃油" in query:
        out["energy_type"] = "ICE"
    elif "新能源" in query:
        out["new_energy"] = True
    upper = query.upper()
    if "SUV" in upper:
        out["body_type"] = "suv"
    elif "MPV" in upper:
        out["body_type"] = "mpv"
    elif "轿车" in query:
        out["body_type"] = "sedan"
    elif "皮卡" in query:
        out["body_type"] = "pickup"
    m = _SEATS_RE.search(query)
    if m:
        out["passengers"] = int(m.group(1))
    return out


def _balance_by_series(results: list[SearchResult], anchor_ids: list[int]) -> list[SearchResult]:
    """对比类双侧均衡（评测 v4）：各锚定车系的证据按名次交错，保证 top_k 内双侧都在场。

    只交错、不丢弃：全部证据仍按原相对名次保留，仅重排前 top_k 的构成——
    修复 pair-coverage 0.439（top_k 被单侧切片挤占，另一侧证据缺席）。
    """
    anchors = [sid for sid in anchor_ids if sid is not None]
    if len(anchors) < 2 or len(results) <= 1:
        return results
    queues: dict[int, list[SearchResult]] = {sid: [] for sid in anchors}
    rest: list[SearchResult] = []
    for hit in results:
        if hit.series_id in queues:
            queues[hit.series_id].append(hit)
        else:
            rest.append(hit)
    ordered: list[SearchResult] = []
    while any(queues.values()):
        for sid in anchors:
            if queues[sid]:
                ordered.append(queues[sid].pop(0))
    return ordered + rest


def _reorder_by_constraints(
    db, results: list[SearchResult], constraints: dict
) -> list[SearchResult]:
    """未点名车系的推荐/语义查询：满足硬约束的证据优先（稳定重排，组内保持原名次）。"""
    sids = {h.series_id for h in results if h.series_id is not None}
    if not sids:
        return results
    from app.catalog.series_constraints import load_series_attrs, series_satisfies

    attrs = load_series_attrs(db, sids)
    valid = [h for h in results if series_satisfies(attrs.get(h.series_id), constraints)]
    invalid = [h for h in results if not series_satisfies(attrs.get(h.series_id), constraints)]
    return valid + invalid


def _analyze(state: RagState) -> RagState:
    """查询理解：车系实体解析 → 增强查询 + 元数据过滤。"""
    started = time.perf_counter()
    query = (state.get("query") or "").strip()
    filters = dict(state.get("filters") or {})
    top_k = min(max(int(state.get("top_k") or 5), 1), 10)
    recall_k = min(max(top_k * max(RECALL_MULTIPLIER, 1), _RECALL_FLOOR), _RECALL_CEILING)

    resolved_names: list[str] = []
    resolved: list = []
    query_type = "semantic"
    warnings: list[str] = []
    if query:
        try:
            resolved = resolve_series(state["db"], query)
        except Exception as err:  # noqa: BLE001 - 实体解析失败不阻断检索
            resolved = []
            warnings.append(f"车系解析失败（按无实体处理）：{err}")
        resolved_names = [display_name(s, b) for s, b in resolved]
        query_type = _classify_query(query, len(resolved))
        # 单车系解析 → 自动加 series_id 过滤（抑制「语义近但实体错」噪声）；
        # 对比类查询**不加**——多实体场景按其中一个子串解析结果过滤，
        # 会把另一个被比对象的整系证据排除（实测 compare 桶 Hit@5 1.0→0.835 的根因）
        if len(resolved) == 1 and "series_id" not in filters and query_type != "compare":
            filters["series_id"] = resolved[0][0].id

    # 实体增强：单车系解析时把规范车系名并入查询（别名/口语 → 索引用语）；
    # 多实体（对比类）不追加——原文已含全部实体，追加品牌词只会稀释双款型精确匹配
    entity_query = query
    if len(resolved) == 1:
        extra = [n for n in resolved_names if n and n.lower() not in query.lower()]
        if extra:
            entity_query = f"{query} {' '.join(extra)}".strip()

    # 优化⑥：领域同义扩展（扩展串只服务稠密路与重排兜底，稀疏路用 entity_query）
    search_query, expanded = expand_query(entity_query)

    return {
        "query": query,
        "filters": filters,
        "top_k": top_k,
        "recall_k": recall_k,
        "entity_query": entity_query,
        "search_query": search_query,
        "query_type": query_type,
        "resolved_series": resolved_names,
        # 评测 v4：锚定车系（compare 双侧均衡）+ 文本解析硬约束（未点名车系的约束下推）
        "anchor_series_ids": [s.id for s, _ in resolved],
        "constraints": _parse_constraints(query) if not resolved else {},
        "stages": [make_stage("analyze", 1, len(resolved_names), started,
                              {"resolved_series": resolved_names, "filters": filters,
                               "recall_k": recall_k, "query_type": query_type,
                               "constraints": _parse_constraints(query) if not resolved else {},
                               "expanded_terms": expanded})],
        "warnings": warnings,
    }


def _route_after_analyze(state: RagState) -> list[str] | str:
    """空查询直接终止；否则并行进入两路召回。

    优化④的参数快路不在这里裁剪分支（join 边要求两分支都完成），
    而是让 _recall_dense 对参数查询空转跳过（等价效果、零 join 风险）。
    """
    if not state.get("search_query") or not tokenize(state["search_query"]):
        return END
    return ["recall_sparse", "recall_dense"]


def _recall_sparse(state: RagState) -> RagState:
    """稀疏召回：进程内 BM25（始终可用）。

    优化⑥ A/B 修正：稀疏路用 **entity_query（不含同义扩展）**——BM25 是词面精确
    匹配，扩展词会把其他车系的同键切片拉进候选、稀释锚点匹配（实测 Hit@5
    0.6718→0.643）；扩展后的 search_query 只服务稠密路（语义召回受益于上下文）。
    """
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
    """稠密召回：Zilliz/Milvus 向量检索（未配置时空转；失败降级为纯稀疏）。

    优化⑥ HyDE：语义类查询可由 LLM 生成假设性证据文本替代原查询做向量召回
    （RETRIEVAL_HYDE 开关，默认关）；生成失败静默回退原查询。
    """
    from app.rag.service import get_dense_backend

    started = time.perf_counter()
    backend = get_dense_backend()
    hits: list[SearchResult] = []
    warnings: list[str] = []
    detail: dict = {"backend": None}
    if state.get("query_type") in ("parameter", "compare"):
        # 优化④：实体锚定查询（参数/对比）只走稀疏快路——键值模板/款型名强区分，
        # 稠密候选「语义近但实体错」只会稀释精确匹配（520 题实测 compare 桶
        # dense 单路 0.873 vs sparse 1.0；跳过还省 embedding 调用与云端延迟）
        detail["skipped"] = f"{state['query_type']}_query"
        return {
            "dense_hits": [],
            "stages": [make_stage("recall_dense", 0, 0, started, detail)],
        }
    if backend is not None:
        detail["backend"] = backend.name
        dense_query = state["search_query"]
        if HYDE_ENABLED and state.get("query_type") == "semantic":
            hyde_text = _hyde_text(state)
            if hyde_text:
                dense_query = hyde_text
                detail["hyde"] = True
        try:
            hits = backend.search(
                dense_query, filters=state.get("filters"), top_k=state["recall_k"]
            )
        except Exception as err:  # noqa: BLE001 - 云端不可用不阻断检索（原则 7）
            warnings.append(f"dense 召回失败（降级为纯稀疏）：{type(err).__name__}: {err}")
            detail["error"] = str(err)[:200]
    return {
        "dense_hits": hits,
        "stages": [make_stage("recall_dense", 1, len(hits), started, detail)],
        "warnings": warnings,
    }


def _hyde_text(state: RagState) -> str | None:
    """HyDE（优化⑥）：LLM 生成一段「理想证据」文本用于向量召回。

    在流水线线程（无事件循环）里同步调用；LLM 未配置/任何失败返回 None，
    调用方静默回退原查询——HyDE 失败绝不阻断检索（原则 7）。
    """
    try:
        import asyncio

        from app.common.llm import LLMClient

        client = LLMClient()
        if not client.available:
            return None
        prompt = (
            "你是汽车参数库的检索助写器。针对用户问题，直接写一段可能出现在车型参数文档里的"
            "事实性描述（50字内，只含车系名与参数键值，不要解释、不要列表）：\n"
            f"用户问题：{state.get('query') or ''}"
        )
        reply = asyncio.run(
            client.chat([{"role": "user", "content": prompt}], temperature=0.1)
        )
        text = ""
        if isinstance(reply, dict):
            choices = reply.get("choices") or []
            if choices:
                text = ((choices[0].get("message") or {}).get("content") or "").strip()
        return text[:300] or None
    except Exception:  # noqa: BLE001 - HyDE 失败不阻断检索
        return None


def _fuse(state: RagState) -> RagState:
    """RRF 融合两路召回（优化⑤：统一 0.6/0.4 加权，消融实测最优）；去重（rrf_fuse 内建）。"""
    started = time.perf_counter()
    sparse = state.get("sparse_hits") or []
    dense = state.get("dense_hits") or []
    query_type = state.get("query_type") or ""
    w_sparse, w_dense = RRF_WEIGHT_SPARSE, RRF_WEIGHT_DENSE
    deduped = rrf_fuse([sparse, dense], weights=[w_sparse, w_dense])
    return {
        "fused": deduped,
        "stages": [make_stage("fuse", len(sparse) + len(dense), len(deduped), started,
                              {"sparse": len(sparse), "dense": len(dense), "rrf_k": RRF_K,
                               "weights": [w_sparse, w_dense], "query_type": query_type})],
    }


def _rerank(state: RagState) -> RagState:
    """精排：Cross-Encoder API（配置时）→ 失败回退 lexical → none = 保持融合序。

    重排按查询类型条件启用（520 题两轮实测收敛）：实体锚定查询（parameter/
    compare）的 BM25 融合序已近最优，Cross-Encoder 中性偏负且白付 ~1s/题
    （compare 桶 Hit@5 0.987→0.975、parameter NDCG 同降）；收益集中在
    recommend/semantic（recommend Hit@5 +1.85pt）——只有这两类调用重排器。
    """
    started = time.perf_counter()
    fused = state.get("fused") or []
    top_k = state["top_k"]
    warnings: list[str] = []
    query_type = state.get("query_type") or ""
    if query_type in ("parameter", "compare"):
        reranker = PassThroughReranker()
    else:
        reranker = get_reranker()
    try:
        ranked = reranker.rerank(state["entity_query"], fused, top_k)
    except Exception as err:  # noqa: BLE001 - 重排服务故障不阻断检索
        warnings.append(f"重排失败（回退融合序）：{type(err).__name__}: {err}")
        reranker = PassThroughReranker()
        ranked = reranker.rerank(state["entity_query"], fused, top_k)
    return {
        "reranked": ranked,
        # grade 节点据此决定阈值语义：只有 Cross-Encoder 的绝对相关分可阈值化
        "reranker_absolute": bool(getattr(reranker, "absolute_scores", False)),
        "stages": [make_stage("rerank", len(fused), len(ranked), started,
                              {"reranker": reranker.name, "query_type": query_type})],
        "warnings": warnings,
    }


def _grade(state: RagState) -> RagState:
    """证据把关：丢弃空文本；重排分具备绝对语义时应用相关性阈值。

    评测 v4 两项信息需求对齐的排序修正（意图互斥，只在明确条件下生效）：
    - compare 双侧均衡：对比类查询两侧锚定车系的证据按名次交错，避免 top_k 被单侧
      挤占（pair-coverage 0.439 的根因）；
    - 约束下推：未点名车系 + 文本解析出硬约束（预算/能源/车身/座位）时，满足约束的
      证据优先（valid-precision 0.445 的根因）。点名车系的车系问答不受影响。
    """
    started = time.perf_counter()
    ranked = state.get("reranked") or []
    absolute = bool(state.get("reranker_absolute"))
    threshold = RELEVANCE_THRESHOLD if absolute else 0.0
    results = [
        hit for hit in ranked
        if hit.text and hit.text.strip() and (threshold <= 0 or hit.score >= threshold)
    ]
    dropped = len(ranked) - len(results)
    reorder = None
    if state.get("query_type") == "compare":
        results = _balance_by_series(results, state.get("anchor_series_ids") or [])
        reorder = "compare_balance"
    elif state.get("constraints") and not state.get("resolved_series"):
        results = _reorder_by_constraints(state["db"], results, state["constraints"])
        reorder = "constraint_first"
    final = results[: state["top_k"]]
    return {
        "results": final,
        "stages": [make_stage("grade", len(ranked), len(final), started,
                              {"dropped": dropped, "threshold": threshold if absolute else None,
                               "reorder": reorder})],
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
