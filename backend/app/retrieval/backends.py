"""检索召回后端：进程内 BM25（稀疏召回）与 Milvus/Zilliz（稠密召回）。

两者实现同一接口，由 app/rag 流水线编排为多路召回（sparse ∥ dense → RRF 融合）；
本地开发无需 Milvus 也能完整运行——稀疏路始终可用。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Protocol, runtime_checkable

# ── 分词（优化③：jieba 中文整词 + 车圈领域词表；不可用时 CJK 二元组兜底）────────
_STATIC_WORDS: tuple[str, ...] = tuple(
    line.strip()
    for line in resources.files("app.retrieval").joinpath("domain_words.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
# 运行期注册词（车系/品牌展示名，ingest 时注入）：专名整词命中显著提升 IDF 精度
_DYNAMIC_WORDS: set[str] = set()
_JIEBA: Any = False  # False=未尝试；None=已尝试但不可用；模块对象=可用


def register_tokens(words: list[str] | set[str]) -> None:
    """注册领域动态词（车系/品牌名等专名），索引构建前调用（优化③）。

    jieba 已加载时同步 add_word；未加载时仅记录（fallback 二元组分词无需词典）。
    """
    for w in words:
        w = (w or "").strip()
        if not w:
            continue
        _DYNAMIC_WORDS.add(w)
        jieba_mod = _get_jieba()
        if jieba_mod is not None:
            jieba_mod.add_word(w, freq=10_000_000)


def _get_jieba():
    """懒加载 jieba；不可用时返回 None（tokenize 自动回退二元组，稀疏路不中断）。"""
    global _JIEBA
    if _JIEBA is False:
        try:
            import jieba

            jieba.setLogLevel(60)
            for w in _STATIC_WORDS:
                jieba.add_word(w, freq=1_000_000)
            for w in _DYNAMIC_WORDS:
                jieba.add_word(w, freq=10_000_000)
            _JIEBA = jieba
        except ImportError:
            _JIEBA = None
    return _JIEBA


from app.retrieval.config import TOKENIZER

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
    """中英文混合分词：英文/数字词元 + 中文词（口径由 RETRIEVAL_TOKENIZER 决定，优化③）。

    - bigram（默认，A/B 实测最优）：汉字二元组——对词表不匹配天然鲁棒；
    - jieba：整词（领域词表 + 动态专名），精度高但召回受词表匹配限制；
    - hybrid：整词 + 二元组。
    索引与查询共用同一函数——分词口径一致是 BM25 召回的前提。
    """
    tokens: list[str] = []
    for m in _WORD_RE.finditer(text):
        tokens.append(m.group(0).lower())
    mode = TOKENIZER if TOKENIZER in ("bigram", "jieba", "hybrid") else "bigram"
    jieba_mod = _get_jieba() if mode in ("jieba", "hybrid") else None
    if jieba_mod is not None:
        # 只把 CJK 连续段交给 jieba（英文/数字已由 _WORD_RE 覆盖，避免重复计数）
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            for w in jieba_mod.lcut(run):
                if len(w) >= 2 and _CJK_RE.match(w):
                    tokens.append(w)
    if jieba_mod is None or mode == "hybrid":
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
        elif key in chunk.extra:
            # extra 携带的元数据（status/price_cny/body_type 等）：切片未携带该键时
            # 不因过滤被误杀（系列切片无 status，但描述的就是当前在售车系）
            if chunk.extra.get(key) != value:
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
