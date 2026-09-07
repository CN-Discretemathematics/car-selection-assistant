"""Zilliz Cloud（serverless）REST 向量库客户端 + OpenAI 兼容 embedding。

为什么用 REST 而非 pymilvus：Zilliz serverless 集群只需 Public Endpoint + Token 即可通过
REST v2 完成建集合/写入/检索，免装 pymilvus、无需手动管理连接；首次写入自动建集合。Embedding 走 OpenAI 兼容接口（`{base}/v1/embeddings`），
可由 Zilliz 官方接口或硅基流动等提供，统一在 app/retrieval/config.py 配置。

生产安全：Token / API Key 只经 .env 或云 KMS 注入，绝不写入代码或仓库（审查红线）。
"""
from __future__ import annotations

import time
from hashlib import md5
from typing import Any, Callable

import httpx

from app.retrieval.backends import SearchChunk, SearchResult
from app.retrieval.config import (
    COLLECT_OP_RETRY_BASE_SECONDS,
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EMBED_RETRY_SECONDS,
    MILVUS_COLLECTION,
    MILVUS_DIM,
    MILVUS_TOKEN,
    MILVUS_URI,
)


class OpenAICompatibleEmbedder:
    """OpenAI 兼容 /v1/embeddings 客户端（Zilliz 官方接口 / 硅基流动 / 阿里百炼 / 智谱均适用）。"""

    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int | None = None,
                 timeout: float = 30.0, retry_seconds: float = EMBED_RETRY_SECONDS) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._timeout = timeout
        self._retry_seconds = retry_seconds

    def _client_factory(self) -> httpx.Client:
        # trust_env=False：直连目标服务（云端部署无代理；本地沙箱代理与 Zilliz 的 TLS 不兼容）
        return httpx.Client(base_url=self._base_url, timeout=self._timeout, trust_env=False)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key or not self._base_url:
            raise RuntimeError("embedding 未配置：需要 EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL")
        payload: dict = {"model": self._model, "input": texts}
        if self._dimensions:
            payload["dimensions"] = self._dimensions  # 阿里百炼 text-embedding-v3 等支持指定维度
        last_err: Exception | None = None
        for attempt in range(1, 4):
            with self._client_factory() as client:
                resp = client.post(
                    "/v1/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            if resp.status_code == 200:
                data = resp.json()["data"]
                return [item["embedding"] for item in sorted(data, key=lambda d: d.get("index", 0))]
            msg = f"embedding 请求失败 {resp.status_code}: {resp.text[:200]}"
            transient = resp.status_code == 429 or "throttl" in resp.text.lower() or "quota" in resp.text.lower() or "rate" in resp.text.lower()
            last_err = RuntimeError(msg)
            if attempt < 3 and transient:
                time.sleep(self._retry_seconds * attempt)  # 限流退避后重试（间隔可由环境变量配置）
                continue
            break
        raise last_err  # type: ignore[misc]


def make_embedder() -> OpenAICompatibleEmbedder:
    """按配置构造 embedding 客户端（OpenAI 兼容 /v1/embeddings，与生产检索共用同一构造路径）。"""
    return OpenAICompatibleEmbedder(
        base_url=EMBEDDING_BASE_URL,
        api_key=EMBEDDING_API_KEY,
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS if EMBEDDING_DIMENSIONS and EMBEDDING_DIMENSIONS.isdigit() else None,
    )


class ZillizRestRetriever:
    """Zilliz Cloud REST v2 检索后端（建集合/写入/检索，元数据过滤）。"""

    name = "zilliz"

    def __init__(
        self,
        endpoint: str | None = None,
        token: str | None = None,
        collection: str | None = None,
        embedder: OpenAICompatibleEmbedder | None = None,
        dim: int | None = None,
    ) -> None:
        self._endpoint = (endpoint or MILVUS_URI).rstrip("/")
        self._token = token if token is not None else MILVUS_TOKEN
        self._collection = collection or MILVUS_COLLECTION
        self._embedder = embedder or make_embedder()
        self._dim = dim or MILVUS_DIM

    @property
    def available(self) -> bool:
        return bool(self._endpoint and self._token)

    def _client_factory(self) -> httpx.Client:
        # trust_env=False：直连目标服务（云端部署无代理；本地沙箱代理与 Zilliz 的 TLS 不兼容）
        return httpx.Client(base_url=self._endpoint, timeout=30.0, trust_env=False)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
        if not self.available:
            raise RuntimeError("Milvus/Zilliz 未配置：需要 MILVUS_URI 与 MILVUS_TOKEN")
        with self._client_factory() as client:
            client.timeout = timeout
            resp = client.request(method, path, headers=self._headers(), json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Zilliz REST {path} 失败 {resp.status_code}: {resp.text[:200]}")
            return resp.json()

    def _schema(self) -> dict:
        return {
            "collectionName": self._collection,
            "dimension": self._dim,
            "metricType": "COSINE",
            "autoID": True,
            "enableDynamicField": False,
            "fields": [
                {"fieldName": "id", "dataType": "Int64", "isPrimary": True, "autoID": True},
                {"fieldName": "text", "dataType": "VarChar", "maxLength": 8192},
                {"fieldName": "vector", "dataType": "FloatVector", "dimension": self._dim},
                {"fieldName": "meta", "dataType": "JSON"},
            ],
        }

    EMBED_BATCH = 10    # 阿里百炼 text-embedding-v3 单次请求上限为 10 条文本
    INSERT_BATCH = 200  # 单次 insert 请求的行数上限（向量维度高，控制请求体大小）

    COLLECT_OP_TIMEOUT = 90.0  # Serverless 集群冷启动慢，集合管理操作放宽超时

    def _retry_collect_op(
        self, method: str, path: str, payload: dict, attempts: int = 5,
        base_delay: float | None = None,
    ) -> dict:
        """集合管理操作重试：serverless 冷启动/负载高时会返回 408 request timeout
        （且操作可能已生效，见「分布式超时」），按线性退避（8/16/24/32s）重试幂等操作。
        退避基数可由 RETRIEVAL_RETRY_BASE_SECONDS 配置（评审 M-M7-1）。"""
        delay = COLLECT_OP_RETRY_BASE_SECONDS if base_delay is None else base_delay
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._request(method, path, payload, timeout=self.COLLECT_OP_TIMEOUT)
            except RuntimeError as err:
                msg = str(err)
                transient = "408" in msg or "10001" in msg or "timeout" in msg.lower() or "dropping" in msg.lower()
                if attempt < attempts and transient:
                    last = err
                    time.sleep(delay * attempt)
                    continue
                raise
        raise last  # type: ignore[misc]  # 防御性兜底：循环内必然 return 或 raise，不会到达

    def _ensure_collection(self) -> None:
        """幂等建集合：已存在则先删除再重建（索引构建=全量重建语义）。

        408/超时视为瞬态：drop 可能已生效但响应超时（分布式超时），
        重试后以「集合不存在」为准继续 create。
        """
        resp: dict | None
        try:
            resp = self._retry_collect_op(
                "POST", "/v2/vectordb/collections/describe", {"collectionName": self._collection}
            )
        except RuntimeError as err:
            if "不存在" in str(err) or "not exist" in str(err).lower() or "100" in str(err):
                resp = None  # 集合不存在，直接创建
            else:
                raise
        if resp is not None and resp.get("code") in (0, 200, None):
            try:
                self._retry_collect_op(
                    "POST", "/v2/vectordb/collections/drop", {"collectionName": self._collection}
                )
            except RuntimeError as err:
                # drop 已生效但响应超时：容忍「集合不存在」
                if "不存在" not in str(err) and "not exist" not in str(err).lower() and "100" not in str(err):
                    raise
        self._retry_collect_op(
            "POST", "/v2/vectordb/collections/create", self._schema()
        )

    def _row(self, text: str, vector: list[float], chunk: SearchChunk) -> dict:
        return {
            "text": text,
            "vector": vector,
            "meta": {
                # chunk_id 随 meta 存储：稠密召回结果与稀疏路（BM25）在 RRF 融合时
                # 按同一 chunk_id 去重（旧集合无此字段时检索侧回退文本哈希）
                "chunk_id": chunk.chunk_id,
                "kind": chunk.kind,
                "brand_id": chunk.brand_id,
                "series_id": chunk.series_id,
                "model_year_id": chunk.model_year_id,
                "variant_id": chunk.variant_id,
                "source_id": chunk.source_id,
                "source_url": chunk.source_url,
                "energy_types": chunk.extra.get("energy_types") or [],
            },
        }

    def index(
        self,
        chunks: list[SearchChunk],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if not chunks:
            return
        self._ensure_collection()
        texts = [c.text for c in chunks]
        rows: list[dict] = []
        for i in range(0, len(texts), self.EMBED_BATCH):
            batch_texts = texts[i : i + self.EMBED_BATCH]
            vectors = self._embedder.embed(batch_texts)
            if len(vectors) != len(batch_texts):
                raise RuntimeError(f"embedding 返回数量不符：{len(vectors)} != {len(batch_texts)}")
            for j, vec in enumerate(vectors):
                rows.append(self._row(batch_texts[j], vec, chunks[i + j]))
            if on_progress:
                on_progress(min(i + self.EMBED_BATCH, len(texts)), len(texts))
            time.sleep(0.05)  # 平滑请求节奏，避免触发 embedding 服务限流（429 由 embed 内重试兜底）
        for i in range(0, len(rows), self.INSERT_BATCH):
            batch_rows = rows[i : i + self.INSERT_BATCH]
            resp = self._request(
                "POST",
                "/v2/vectordb/entities/insert",
                {"collectionName": self._collection, "data": batch_rows},
            )
            if resp.get("code") not in (0, 200, None):
                raise RuntimeError(f"Zilliz 写入返回错误码 {resp.get('code')}: {str(resp)[:200]}")
            if on_progress:
                on_progress(min(i + self.INSERT_BATCH, len(rows)), len(rows))

    @staticmethod
    def _quote(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def _filter_expr(self, filters: dict[str, Any] | None) -> str | None:
        parts: list[str] = []
        for key, value in (filters or {}).items():
            if key == "energy_type":
                parts.append(f'json_contains(meta["energy_types"], {self._quote(value)})')
            elif isinstance(value, int):
                parts.append(f'meta["{key}"] == {value}')
            else:
                parts.append(f'meta["{key}"] == {self._quote(value)}')
        return " and ".join(parts) if parts else None

    def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        vector = self._embedder.embed([query])[0]
        payload: dict[str, Any] = {
            "collectionName": self._collection,
            "data": [vector],
            "limit": top_k,
            "outputFields": ["text", "meta"],
        }
        expr = self._filter_expr(filters)
        if expr:
            payload["filter"] = expr
        resp = self._request("POST", "/v2/vectordb/entities/search", payload)
        if resp.get("code") not in (0, 200, None):
            raise RuntimeError(f"Zilliz 检索返回错误码 {resp.get('code')}: {str(resp)[:200]}")
        entities = resp.get("data") or []
        if isinstance(entities, dict):
            entities = entities.get("entities") or []
        elif isinstance(entities, list) and entities and isinstance(entities[0], dict) and "entities" in entities[0]:
            entities = entities[0]["entities"]
        results: list[SearchResult] = []
        for hit in entities:
            meta = hit.get("meta") or {}
            text = hit.get("text") or ""
            # 旧集合 meta 无 chunk_id：回退文本哈希，保证融合去重仍可用（重建索引后恢复精确 id）
            chunk_id = meta.get("chunk_id") or f"z{md5(text.encode('utf-8')).hexdigest()[:16]}"
            results.append(
                SearchResult(
                    chunk_id=str(chunk_id),
                    score=round(float(hit.get("distance") or 0.0), 4),
                    text=text,
                    kind=meta.get("kind") or "",
                    brand_id=meta.get("brand_id"),
                    series_id=meta.get("series_id"),
                    variant_id=meta.get("variant_id"),
                    source_id=meta.get("source_id"),
                    source_url=meta.get("source_url"),
                )
            )
        return results
