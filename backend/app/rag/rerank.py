"""融合与重排（主流两阶段检索的第二阶段）。

- RRF（Reciprocal Rank Fusion，k=60）：多路召回的标准融合算法，无量纲、免调参，
  对稀疏（BM25）与稠密（向量）两路分数不可比的问题天然免疫；
- LexicalReranker：查询-切片词元重叠加权（默认，零外部依赖），缓解
  「语义近但实体错」的召回噪声；
- CrossEncoderReranker：Cross-Encoder 重排服务（硅基流动 BAAI/bge-reranker-v2-m3、
  Jina、Cohere 兼容的 POST {base}/rerank 协议）。重排是精排阶段的主流做法，
  配置后自动启用；调用失败自动回退 lexical，检索不中断（原则 7）。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Protocol

import httpx

from app.retrieval.backends import SearchResult, tokenize
from app.retrieval.config import (
    LEXICAL_RERANK_BOOST,
    LEXICAL_RERANK_CAP,
    RERANK_API_KEY,
    RERANK_BASE_URL,
    RERANK_MODEL,
    RERANK_PROVIDER,
    RERANK_TIMEOUT_SECONDS,
    RRF_K,
)


def rrf_fuse(rank_lists: list[list[SearchResult]], k: int = RRF_K) -> list[SearchResult]:
    """RRF 融合多路召回：score(d) = Σ 1/(k + rank_i(d))。

    两级去重：chunk_id（同片被多路召回时名次贡献叠加，即共识加权）+ 文本兜底
    （旧 dense 集合缺 meta.chunk_id 时与稀疏路 id 不一致；同文本证据本就冗余）。
    保留原始命中信息，score 替换为融合分（下游重排/阈值以融合分为基准）。
    """
    fused: dict[str, float] = {}
    best: dict[str, SearchResult] = {}
    for hits in rank_lists:
        for rank, hit in enumerate(hits):
            fused[hit.chunk_id] = fused.get(hit.chunk_id, 0.0) + 1.0 / (k + rank + 1)
            prev = best.get(hit.chunk_id)
            if prev is None or hit.score > prev.score:
                best[hit.chunk_id] = hit
    ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    seen_text: set[str] = set()
    out: list[SearchResult] = []
    for chunk_id, score in ordered:
        hit = best[chunk_id]
        key = hit.text.strip()
        if key in seen_text:
            continue
        seen_text.add(key)
        out.append(replace(hit, score=round(score, 6)))
    return out


class Reranker(Protocol):
    name: str

    def rerank(self, query: str, results: list[SearchResult], top_k: int) -> list[SearchResult]: ...


class LexicalReranker:
    """词元重叠加权重排：查询与切片共享的命名/数值 token 越多越靠前。

    token 化复用召回层的 tokenize（英文/数字词元 + CJK 二元组），
    加分 = min(重叠数 * boost, cap)，叠加在召回/融合分之上。
    """

    name = "lexical"

    def __init__(self, boost: float = LEXICAL_RERANK_BOOST, cap: float = LEXICAL_RERANK_CAP) -> None:
        self._boost = boost
        self._cap = cap

    def rerank(self, query: str, results: list[SearchResult], top_k: int) -> list[SearchResult]:
        if not results:
            return []
        tokens = set(tokenize(query))
        if not tokens:
            return results[:top_k]
        scored: list[tuple[float, int, SearchResult]] = []
        for i, hit in enumerate(results):
            text = (hit.text or "").lower()
            overlap = sum(1 for t in tokens if t in text)
            scored.append((hit.score + min(overlap * self._boost, self._cap), i, hit))
        # 分数相同保持融合序（稳定排序按原下标）
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [replace(hit, score=round(score, 6)) for score, _, hit in scored[:top_k]]


class CrossEncoderReranker:
    """Cross-Encoder 重排 API 客户端（SiliconFlow / Jina / Cohere 兼容协议）。

    请求：POST {base_url}/rerank  {"model", "query", "documents", "top_n"}
    响应：{"results": [{"index": int, "relevance_score": float}, ...]}
    relevance_score 是绝对相关分，可配合 RETRIEVAL_RELEVANCE_THRESHOLD 过滤低相关证据。
    """

    name = "cross_encoder"

    def __init__(
        self,
        base_url: str = RERANK_BASE_URL,
        api_key: str = RERANK_API_KEY,
        model: str = RERANK_MODEL,
        timeout: float = RERANK_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        # 标记分数语义：grade 节点只对绝对相关分应用阈值
        self.absolute_scores = True

    def _post(self, payload: dict) -> dict:
        # trust_env=False：直连目标服务（与 embedding/Zilliz 客户端一致，避开本地代理 TLS 问题）
        with httpx.Client(timeout=self._timeout, trust_env=False) as client:
            resp = client.post(
                f"{self._base_url}/rerank",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"rerank 请求失败 {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def rerank(self, query: str, results: list[SearchResult], top_k: int) -> list[SearchResult]:
        if not results:
            return []
        documents = [hit.text for hit in results]
        payload = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": top_k,
        }
        items = self._post(payload).get("results") or []
        ranked: list[SearchResult] = []
        for item in items:
            idx = item.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(results)):
                continue
            score = float(item.get("relevance_score") or 0.0)
            ranked.append(replace(results[idx], score=round(score, 6)))
        if not ranked:
            raise RuntimeError("rerank 响应无有效结果")
        return ranked


_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    """按配置选择重排器（进程级单例）：api → CrossEncoderReranker，否则 lexical。"""
    global _reranker
    if _reranker is None:
        if RERANK_PROVIDER == "api" and RERANK_BASE_URL and RERANK_API_KEY and RERANK_MODEL:
            _reranker = CrossEncoderReranker()
        else:
            _reranker = LexicalReranker()
    return _reranker


def reset_reranker() -> None:
    """测试辅助：清空重排器单例。"""
    global _reranker
    _reranker = None
