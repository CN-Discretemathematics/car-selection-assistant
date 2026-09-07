"""Zilliz 真实联调验证（不打印任何密钥）。

两阶段：
1. 向量库全流程（真实集群）：建集合 → 写入 → 检索 → 元数据过滤；
   沙箱环境无法访问 api.zillizcloud.com（embedding），此阶段用确定性伪向量
   （维度与 MILVUS_DIM 一致），验证的是 Zilliz 集群本身的连通与 schema。
2. embedding 可达性：真实调用 EMBEDDING_* 配置，失败时给出原因提示。
   本机/云端部署无沙箱代理限制，embedding 将正常工作。

用法：python tools/verify_zilliz.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.retrieval.backends import SearchChunk  # noqa: E402
from app.retrieval.config import (  # noqa: E402
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    MILVUS_DIM,
    MILVUS_URI,
    RETRIEVAL_BACKEND,
)
from app.retrieval.zilliz import ZillizRestRetriever  # noqa: E402


class FakeEmbedder:
    """确定性伪向量（维度与集合一致），仅用于验证 Zilliz 集群连通性。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[(hash(t) % 1000) / 1000.0 for _ in range(MILVUS_DIM)] for t in texts]


def main() -> int:
    print(f"RETRIEVAL_BACKEND={RETRIEVAL_BACKEND}  MILVUS_URI={'已配置' if MILVUS_URI else '空'}  "
          f"EMBEDDING_PROVIDER={EMBEDDING_PROVIDER}  MODEL={EMBEDDING_MODEL}  DIM={MILVUS_DIM}")

    retriever = ZillizRestRetriever(embedder=FakeEmbedder())
    if not retriever.available:
        print("失败：MILVUS_URI / MILVUS_TOKEN 未配置")
        return 1

    chunks = [
        SearchChunk(
            chunk_id="verify-1",
            text="家用SUV 大空间 五座 适合家庭出行",
            kind="series_intro",
            series_id=1,
            extra={"energy_types": ["BEV"]},
        ),
        SearchChunk(
            chunk_id="verify-2",
            text="通勤家轿 低油耗 适合上下班通勤",
            kind="series_intro",
            series_id=2,
            extra={"energy_types": ["ICE"]},
        ),
    ]
    print("阶段 1/2：真实 Zilliz 集群 建集合 + 写入 2 条测试切片…")
    try:
        retriever.index(chunks)
    except Exception as err:  # noqa: BLE001
        print(f"  失败：{type(err).__name__}: {err}")
        return 1
    print("  写入成功")

    print("  检索（全量，伪向量按文本散列）…")
    try:
        results = retriever.search("家庭出行 空间", top_k=2)
    except Exception as err:  # noqa: BLE001
        print(f"  失败：{type(err).__name__}: {err}")
        return 1
    if not results:
        print("  检索返回为空（伪向量下正常也可能命中低分），继续过滤验证…")
    else:
        for r in results:
            print(f"  hit: kind={r.kind} series={r.series_id} score={r.score} text={r.text[:36]}")

    print("  元数据过滤 series_id=2 …")
    filtered = retriever.search("适合", filters={"series_id": 2}, top_k=2)
    print(f"  {len(filtered)} 条命中（应全为 series=2）")
    milvus_ok = all(r.series_id == 2 for r in filtered)

    print("阶段 2/2：embedding 可达性（真实 EMBEDDING_* 配置，与生产构造路径一致）…")
    embed_ok = False
    try:
        from app.retrieval.zilliz import make_embedder

        embedder = make_embedder()
        vec = embedder.embed(["测试"])
        print(f"  embedding 成功，向量维度={len(vec[0])}")
        embed_ok = len(vec[0]) == MILVUS_DIM
    except Exception as err:  # noqa: BLE001
        print(f"  失败：{type(err).__name__}: {err}")
        print(
            "  提示：若 PROVIDER=zilliz_builtin，其域名 api.zillizcloud.com 在大陆 DNS 不可达，"
            "且杭州 serverless 集群不支持服务端 TEXTEMBEDDING Function——请切换国内供应商：\n"
            "    EMBEDDING_PROVIDER=openai_compatible\n"
            "    EMBEDDING_BASE_URL=https://api.siliconflow.cn\n"
            "    EMBEDDING_MODEL=BAAI/bge-m3\n"
            "    EMBEDDING_API_KEY=<硅基流动密钥>\n"
            "（硅基流动已实测本机直连可达）"
        )

    conclusion = f"Zilliz 集群 {'PASS' if milvus_ok else 'FAIL'}；embedding {'PASS' if embed_ok else 'FAIL（按提示切换国内供应商）'}"
    print(f"结论：{conclusion}")
    return 0 if milvus_ok and embed_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
