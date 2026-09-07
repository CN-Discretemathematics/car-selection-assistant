"""RAG 编排（LangGraph）。

两条 StateGraph 流水线：
- pipeline.py：查询流水线  analyze → recall(sparse ∥ dense) → fuse(RRF) → rerank → grade；
- ingest.py：摄取流水线    load → chunk → index。

service.py 是唯一门面：search()（Agent 工具）/ try_query()、get_status()、
recent_runs()、graph_spec()（管理后台可视化）。召回后端与向量库客户端仍在
app/retrieval/（backends.py BM25、zilliz.py Milvus REST）。
"""
