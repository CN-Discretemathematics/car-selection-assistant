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
    RERANK_API_PATH,
    RERANK_BASE_URL,
    RERANK_MODEL,
    RERANK_PROVIDER,
    RERANK_TIMEOUT_SECONDS,
    RRF_K,
)


def rrf_fuse(
    rank_lists: list[list[SearchResult]],
    k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[SearchResult]:
    """RRF 融合多路召回：score(d) = Σ w_i / (k + rank_i(d))（优化⑤：支持路权重）。

    两级去重：chunk_id（同片被多路召回时名次贡献叠加，即共识加权）+ 文本兜底
    （旧 dense 集合缺 meta.chunk_id 时与稀疏路 id 不一致；同文本证据本就冗余）。
    weights 与 rank_lists 一一对应（None = 等权 1.0，即经典 RRF）；
    保留原始命中信息，score 替换为融合分（下游重排/阈值以融合分为基准）。
    """
    fused: dict[str, float] = {}
    best: dict[str, SearchResult] = {}
    for li, hits in enumerate(rank_lists):
        w = weights[li] if weights and li < len(weights) else 1.0
        for rank, hit in enumerate(hits):
            fused[hit.chunk_id] = fused.get(hit.chunk_id, 0.0) + w / (k + rank + 1)
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


class PassThroughReranker:
    """不重排：保持融合序直接截断（`RERANK_PROVIDER=none`）。

    A/B 实测（520 题锚点口径）：本语料上 BM25 融合序已近最优，重排层（lexical
    或 cross-encoder）均为轻微负收益——生产可按评测选择 none / lexical / api。
    """

    name = "none"
    absolute_scores = False

    def rerank(self, query: str, results: list[SearchResult], top_k: int) -> list[SearchResult]:
        return results[:top_k]


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
    """Cross-Encoder 重排 API 客户端（优化①：多协议自适应）。

    支持三类协议，按 base_url 自动选择（也可用 RERANK_API_PATH 显式指定路径）：
    - OpenAI 兼容 rerank：POST {base}/reranks（阿里百炼 qwen3.7-text-rerank 等）
      响应 {"results": [{"index", "relevance_score"}]}（兼容层展平 output）；
    - DashScope 原生：POST {base}/services/rerank/text-rerank/text-rerank，
      响应 {"output": {"results": [...]}}；
    - 通用 Cohere 兼容：POST {base}/rerank（硅基流动 BAAI/bge-reranker-v2-m3、Jina）。
    relevance_score 是绝对相关分，可配合 RETRIEVAL_RELEVANCE_THRESHOLD 过滤低相关证据。
    """

    name = "cross_encoder"

    def __init__(
        self,
        base_url: str = RERANK_BASE_URL,
        api_key: str = RERANK_API_KEY,
        model: str = RERANK_MODEL,
        timeout: float = RERANK_TIMEOUT_SECONDS,
        api_path: str = RERANK_API_PATH,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._api_path = (api_path or "").strip()
        self._resolved_path: str | None = None  # 探测成功的路径（进程内复用）
        # 标记分数语义：grade 节点只对绝对相关分应用阈值
        self.absolute_scores = True

    def _candidate_paths(self) -> list[str]:
        if self._api_path:
            return [self._api_path if self._api_path.startswith("/") else "/" + self._api_path]
        base = self._base_url.lower()
        if "dashscope" in base or "maas.aliyuncs.com" in base:
            # 阿里系：先试 OpenAI 兼容 /reranks，失败回退原生 text-rerank
            # （两种协议的文本重排请求体均为平铺 {model, query, documents, top_n}）
            return ["/reranks", "/services/rerank/text-rerank/text-rerank"]
        return ["/rerank"]

    @staticmethod
    def _extract_items(data: dict) -> list[dict]:
        """响应兼容层：results 在顶层（OpenAI/Cohere）或 output 下（DashScope 原生）。"""
        items = data.get("results")
        if items is None and isinstance(data.get("output"), dict):
            items = data["output"].get("results")
        if items is None and isinstance(data.get("data"), list):
            items = data["data"]
        return [it for it in (items or []) if isinstance(it, dict)]

    def _post(self, payload: dict, path: str) -> dict:
        # trust_env=False：直连目标服务（与 embedding/Zilliz 客户端一致，避开本地代理 TLS 问题）
        with httpx.Client(timeout=self._timeout, trust_env=False) as client:
            resp = client.post(
                f"{self._base_url}{path}",
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
        last_err: Exception | None = None
        paths = [self._resolved_path] if self._resolved_path else self._candidate_paths()
        # 对全量候选打分（而非 API 侧截断）：相关性阈值的过滤发生在 grade 节点，
        # 先截断会让低分候选出局后没有替补（corrective-RAG 语义失效）
        n_all = len(documents)
        for path in paths:
            # 按路径塑形：OpenAI 兼容 /reranks 与 Cohere /rerank 为平铺结构；
            # DashScope 原生 text-rerank 要求嵌套 input/parameters（实测 400 校验）
            if path.endswith("/text-rerank/text-rerank"):
                payload = {
                    "model": self._model,
                    "input": {"query": query, "documents": documents},
                    "parameters": {"top_n": n_all, "return_documents": False},
                }
            else:
                payload = {
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": n_all,
                }
            try:
                data = self._post(payload, path)
                items = self._extract_items(data)
                if not items:
                    raise RuntimeError("rerank 响应无有效结果")
            except Exception as err:  # noqa: BLE001 - 尝试下一候选协议
                last_err = err
                continue
            self._resolved_path = path
            ranked: list[SearchResult] = []
            for item in items:
                idx = item.get("index")
                if not isinstance(idx, int) or not (0 <= idx < len(results)):
                    continue
                score = item.get("relevance_score")
                if score is None:
                    score = item.get("score", 0.0)
                ranked.append(replace(results[idx], score=round(float(score or 0.0), 6)))
            if ranked:
                return ranked
            last_err = RuntimeError("rerank 响应 index 全部无效")
        raise last_err  # type: ignore[misc]


_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    """按配置选择重排器（进程级单例）：api → CrossEncoderReranker，none/空 → 不重排，
    lexical → 词元重排。

    默认 none 是 520 题 A/B 的实测结论：本语料（结构化键值 + 锚点式查询）上
    BM25 融合序已近最优，重排层为中性偏负（见 RAG.md §4.2）；语义查询占比高的
    部署可显式切换 api/lexical 并用同一评测复验。
    """
    global _reranker
    if _reranker is None:
        if RERANK_PROVIDER == "api" and RERANK_BASE_URL and RERANK_API_KEY and RERANK_MODEL:
            _reranker = CrossEncoderReranker()
        elif RERANK_PROVIDER == "lexical":
            _reranker = LexicalReranker()
        else:
            _reranker = PassThroughReranker()
    return _reranker


def reset_reranker() -> None:
    """测试辅助：清空重排器单例。"""
    global _reranker
    _reranker = None
