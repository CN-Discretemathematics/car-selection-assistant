"""检索召回后端：进程内 BM25（稀疏召回）与 Milvus/Zilliz（稠密召回）。

两者实现同一接口，由 app/rag 流水线编排为多路召回（sparse ∥ dense → RRF 融合）；
本地开发无需 Milvus 也能完整运行——稀疏路始终可用。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class SearchChunk:
    """文档切片（元数据）。"""

    chunk_id: str
    text: str
    kind: str  # series_intro | series_summary | spec_fact | source_document
    document_id: int | None = None
    brand_id: int | None = None
    series_id: int | None = None
    model_year_id: int | None = None
    variant_id: int | None = None
    source_id: int | None = None
    source_url: str | None = None
    page_or_section: str | None = None
    effective_from: Any = None
    effective_to: Any = None
    last_verified_at: Any = None
    extra: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    chunk_id: str
    score: float
    text: str
    kind: str
    brand_id: int | None = None
    series_id: int | None = None
    variant_id: int | None = None
    source_id: int | None = None
    source_url: str | None = None


def tokenize(text: str) -> list[str]:
    """中英文混合分词：英文/数字词元 + 汉字二元组（BM25 与稀疏检索共用）。"""
    tokens: list[str] = []
    for m in _WORD_RE.finditer(text):
        tokens.append(m.group(0).lower())
    cjk_run: list[str] = []
    for ch in text:
        if _CJK_RE.match(ch):
            cjk_run.append(ch)
        else:
            cjk_run = []
        if len(cjk_run) >= 2:
            tokens.append("".join(cjk_run[-2:]))
    return tokens


class _BM25Index:
    """轻量 BM25 倒排索引（Okapi BM25，k1=1.5，b=0.75）。"""

    K1 = 1.5
    B = 0.75

    def __init__(self) -> None:
        self.chunks: list[SearchChunk] = []
        self.postings: dict[str, dict[int, int]] = {}  # term -> {chunk_pos: tf}
        self.doc_len: list[int] = []
        self.avgdl = 0.0

    def clear(self) -> None:
        self.chunks = []
        self.postings = {}
        self.doc_len = []
        self.avgdl = 0.0

    def add(self, chunk: SearchChunk) -> None:
        pos = len(self.chunks)
        self.chunks.append(chunk)
        tokens = tokenize(chunk.text)
        self.doc_len.append(len(tokens))
        for token in tokens:
            postings = self.postings.setdefault(token, {})
            postings[pos] = postings.get(pos, 0) + 1

    def finalize(self) -> None:
        """索引构建完成后一次性计算平均文档长度（避免 add 时 O(n²) 重算）。"""
        if self.doc_len:
            self.avgdl = sum(self.doc_len) / len(self.doc_len)

    def _idf(self, term: str) -> float:
        df = len(self.postings.get(term, {}))
        n = len(self.chunks)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def score(self, tokens: list[str], pos: int) -> float:
        score = 0.0
        doc_tokens: dict[str, int] = {}
        # 反转 term -> tf
        for term in tokens:
            if pos in self.postings.get(term, {}):
                doc_tokens[term] = self.postings[term][pos]
        for term, tf in doc_tokens.items():
            idf = self._idf(term)
            dl = self.doc_len[pos]
            score += idf * (tf * (self.K1 + 1)) / (tf + self.K1 * (1 - self.B + self.B * dl / self.avgdl))
        return score


@runtime_checkable
class RetrievalBackend(Protocol):
    def index(self, chunks: list[SearchChunk]) -> None: ...

    def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
    ) -> list[SearchResult]: ...


def _match_filters(chunk: SearchChunk, filters: dict[str, Any]) -> bool:
    for key, value in (filters or {}).items():
        if key == "energy_type":
            # 系列切片存列表、SKU 切片存单值：统一按「包含」判断
            energy_types = chunk.extra.get("energy_types") or []
            if isinstance(energy_types, str):
                energy_types = [energy_types]
            if value not in energy_types:
                return False
        elif getattr(chunk, key, None) != value:
            return False
    return True


class InMemoryRetriever:
    """本地 BM25 检索器：稀疏召回路，接口与 Milvus 稠密后端一致。"""

    name = "inmemory"

    def __init__(self) -> None:
        self._index = _BM25Index()

    @property
    def chunks(self) -> list[SearchChunk]:
        """已索引切片（状态统计/评估用）。"""
        return self._index.chunks

    def index(self, chunks: list[SearchChunk]) -> None:
        self._index.clear()
        for chunk in chunks:
            self._index.add(chunk)
        self._index.finalize()

    def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        tokens = tokenize(query)
        if not tokens or not self._index.chunks:
            return []
        scored = [
            (pos, self._index.score(tokens, pos))
            for pos in range(len(self._index.chunks))
            if _match_filters(self._index.chunks[pos], filters)
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        results: list[SearchResult] = []
        for pos, score in scored[:top_k]:
            if score <= 0:
                continue
            chunk = self._index.chunks[pos]
            results.append(
                SearchResult(
                    chunk_id=chunk.chunk_id,
                    score=round(score, 4),
                    text=chunk.text,
                    kind=chunk.kind,
                    brand_id=chunk.brand_id,
                    series_id=chunk.series_id,
                    variant_id=chunk.variant_id,
                    source_id=chunk.source_id,
                    source_url=chunk.source_url,
                )
            )
        return results


def build_backend(backend: str) -> RetrievalBackend:
    if backend == "milvus":
        # 优先：Zilliz Cloud serverless（REST，免 pymilvus）；无 URI/Token 时明确报错
        from app.retrieval.config import MILVUS_TOKEN, MILVUS_URI
        from app.retrieval.zilliz import ZillizRestRetriever

        if MILVUS_URI and MILVUS_TOKEN:
            return ZillizRestRetriever()
        raise RuntimeError("Milvus/Zilliz 后端需要 MILVUS_URI 与 MILVUS_TOKEN（生产经 KMS 注入）")
    return InMemoryRetriever()
