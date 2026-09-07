"""召回后端模块：只负责「取回候选」。

- backends.py：进程内 Okapi BM25 稀疏召回（本地开发/测试与生产稀疏路，接口一致）；
- zilliz.py：Milvus/Zilliz REST v2 稠密向量召回 + OpenAI 兼容 embedding 客户端；
- config.py：检索/切分/召回/融合/重排全部配置项（环境变量注入）。

多路融合（RRF）、重排、把关与流程编排在 `app/rag/`（LangGraph）；
文档切片元数据遵循 §16.3。
"""
