"""Zilliz REST 检索后端与 embedding 测试（httpx MockTransport，无需真实服务）。"""
from __future__ import annotations

import httpx

from app.retrieval.backends import SearchChunk
from app.retrieval.zilliz import OpenAICompatibleEmbedder as Embedder, ZillizRestRetriever


class _FakeEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 1.0] for t in texts]  # 确定性伪向量，便于测试


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_embedder_openai_compatible(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"].startswith("Bearer ")
        # 未指定 dimensions 时请求体必须不含该键（向后兼容承诺）
        assert "dimensions" not in json.loads(request.read().decode("utf-8"))
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3, 0.4]}]},
        )

    embedder = Embedder(base_url="https://api.example.com", api_key="placeholder-key", model="m")
    embedder._client_factory = lambda: httpx.Client(base_url=embedder._base_url, transport=_mock_transport(handler))
    vectors = embedder.embed(["a", "b"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


def test_embedder_sends_dimensions_when_set(monkeypatch):
    """阿里百炼等兼容端点：指定 dimensions 时必须随请求下发。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.read().decode("utf-8")))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 1024}]})

    embedder = Embedder(base_url="https://api.example.com", api_key="placeholder-key", model="text-embedding-v3", dimensions=1024)
    embedder._client_factory = lambda: httpx.Client(base_url=embedder._base_url, transport=_mock_transport(handler))
    embedder.embed(["x"])
    assert seen["dimensions"] == 1024


def test_zilliz_retriever_create_insert_search(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", "replace")
        import json

        payload = json.loads(body) if body else {}
        calls.append((request.method, request.url.path, payload))
        if request.url.path.endswith("/collections/describe"):
            # 首次：集合不存在（HTTP 404 + 错误码 100）
            return httpx.Response(404, json={"code": 100, "message": "collection not exist"})
        if request.url.path.endswith("/collections/drop"):
            return httpx.Response(200, json={"code": 0})
        if request.url.path.endswith("/collections/create"):
            return httpx.Response(200, json={"code": 0})
        if request.url.path.endswith("/entities/insert"):
            return httpx.Response(200, json={"code": 0, "data": {"insertCount": 1}})
        if request.url.path.endswith("/entities/search"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": [
                        {
                            "entities": [
                                {
                                    "id": "1",
                                    "distance": 0.92,
                                    "text": "家用SUV 大空间",
                                    "meta": {"kind": "series_intro", "series_id": 7, "source_id": 3},
                                }
                            ]
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"code": 1, "message": "unknown"})

    retriever = ZillizRestRetriever(
        endpoint="https://in03-x.api.zillizcloud.com",
        token="placeholder-token",
        collection="car_docs",
        embedder=_FakeEmbedder(),
        dim=2,
    )
    retriever._client_factory = lambda: httpx.Client(base_url=retriever._endpoint, transport=_mock_transport(handler))

    chunks = [SearchChunk(chunk_id="series-7", text="家用SUV 大空间", kind="series_intro", series_id=7, extra={"energy_types": ["BEV"]})]
    retriever.index(chunks)

    results = retriever.search("家用 空间", filters={"series_id": 7}, top_k=2)
    assert results and results[0].text == "家用SUV 大空间"
    assert results[0].series_id == 7

    # 校验请求形状与鉴权头
    create = next(c for c in calls if c[1].endswith("/collections/create"))
    assert create[2]["collectionName"] == "car_docs"
    insert = next(c for c in calls if c[1].endswith("/entities/insert"))
    assert insert[2]["collectionName"] == "car_docs"
    assert insert[2]["data"][0]["meta"]["series_id"] == 7
    search = next(c for c in calls if c[1].endswith("/entities/search"))
    assert search[2]["filter"] == 'meta["series_id"] == 7'


def test_zilliz_index_drops_existing_collection(monkeypatch):
    """集合已存在时：先 drop 再 create（全量重建语义）。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.split("/")[-1])
        if request.url.path.endswith("/collections/describe"):
            return httpx.Response(200, json={"code": 0, "data": {"collectionName": "car_docs"}})
        return httpx.Response(200, json={"code": 0})

    retriever = ZillizRestRetriever(
        endpoint="https://in03-x.api.zillizcloud.com",
        token="placeholder-token",
        collection="car_docs",
        embedder=_FakeEmbedder(),
        dim=2,
    )
    retriever._client_factory = lambda: httpx.Client(base_url=retriever._endpoint, transport=_mock_transport(handler))
    retriever.index([SearchChunk(chunk_id="s1", text="测试", kind="series_intro", series_id=1)])
    assert "drop" in calls and "create" in calls
    assert calls.index("drop") < calls.index("create")


def test_zilliz_requires_config():
    retriever = ZillizRestRetriever(endpoint="", token="", embedder=_FakeEmbedder())
    try:
        retriever.search("x")
        raise AssertionError("未配置时应抛 RuntimeError")
    except RuntimeError:
        pass
