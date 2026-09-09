"""检索/RAG 配置（独立于 common/config.py，后续回合合并进 Settings）。

分层：
- 存储与召回：RETRIEVAL_BACKEND / MILVUS_* / EMBEDDING_*；
- 切分：RETRIEVAL_CHUNK_SIZE / RETRIEVAL_CHUNK_OVERLAP（递归切分 + 重叠）；
- 融合与重排：RETRIEVAL_RRF_K / RERANK_* / RETRIEVAL_RELEVANCE_THRESHOLD；
- 流水线：RETRIEVAL_RECALL_MULTIPLIER / RAG_RUN_LOG_SIZE。
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# 应用与工具都从这里读取检索配置：必须先加载 .env（override=False——
# 已存在的真实环境变量优先，生产由 KMS/平台注入的环境变量不会被 .env 覆盖）。
load_dotenv()


def _env_int(name: str, default: int) -> int:
    """int 型配置容错：非数字值回退默认（评审 L3）。"""
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """float 型配置容错：非数字/负数回退默认（评审 M-M7-1）。"""
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


# ── 存储与召回后端 ────────────────────────────────────────────────────────────
RETRIEVAL_BACKEND = os.environ.get("RETRIEVAL_BACKEND", "inmemory")  # inmemory | milvus(->Zilliz REST)
MILVUS_URI = os.environ.get("MILVUS_URI", "")  # Zilliz Public Endpoint
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "")  # Zilliz 集群 Token（生产经 KMS 注入，不写代码/仓库）
MILVUS_COLLECTION = os.environ.get("MILVUS_COLLECTION", "car_docs")
MILVUS_DIM = _env_int("MILVUS_DIM", 1024)
MAX_CHUNKS = _env_int("RETRIEVAL_MAX_CHUNKS", 60000)
# 每款型进入检索索引的最大事实条数（优先级排序后截断；优化②：
# 切分从「单事实切片」改为「款型级合并切片」——命中即对齐 SKU，省证据归属映射）
FACTS_PER_VARIANT = _env_int("RETRIEVAL_FACTS_PER_VARIANT", 30)

# Embedding（OpenAI 兼容接口：Zilliz 官方 / 硅基流动 / 阿里百炼 / 其他）
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "")  # zilliz_builtin | siliconflow | openai_compatible
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
# 可选：指定输出维度（阿里百炼 text-embedding-v3 支持 1024/768/512；留空用服务端默认）
EMBEDDING_DIMENSIONS = os.environ.get("EMBEDDING_DIMENSIONS", "")
# embedding 向量本地缓存（稠密重建断点续跑：配额/网络中断后只补未完成批次；留空关闭）
EMBED_CACHE = os.environ.get("RETRIEVAL_EMBED_CACHE", "").strip()

# ── 切分（递归字符切分，中文友好分隔符，见 app/rag/chunking.py）───────────────
CHUNK_SIZE = _env_int("RETRIEVAL_CHUNK_SIZE", 500)  # 字符
CHUNK_OVERLAP = _env_int("RETRIEVAL_CHUNK_OVERLAP", 64)  # 相邻切片重叠字符，防止句界证据割裂
# 稀疏分词器（优化③，A/B 实测：本语料（结构化键值文本+短查询）下 bigram 最优——
# 整词的词表不匹配召回损失 > 精度收益。jieba/hybrid 保留可切换，jieba 另服务于查询同义扩展）
# bigram | jieba（纯整词） | hybrid（整词+二元组）
TOKENIZER = os.environ.get("RETRIEVAL_TOKENIZER", "bigram").strip().lower()

# ── 多路召回与融合（见 app/rag/pipeline.py）──────────────────────────────────
# 每路召回条数 = top_k * RECALL_MULTIPLIER（先大召回再精排，主流两阶段检索）
RECALL_MULTIPLIER = _env_int("RETRIEVAL_RECALL_MULTIPLIER", 6)
# RRF（Reciprocal Rank Fusion）平滑常数，业界默认 60
RRF_K = _env_int("RETRIEVAL_RRF_K", 60)
# 加权 RRF（优化⑤）：各路名次贡献按权重叠加 score = Σ w/(k+rank)；
# 0 < w <= 1。默认参数查询偏稀疏（键值模板强区分）、语义查询偏稠密，
# 由查询路由（④）在运行期选择权重组，这里定义的默认值用于无路由场景。
RRF_WEIGHT_SPARSE = _env_float("RETRIEVAL_RRF_WEIGHT_SPARSE", 0.6)
RRF_WEIGHT_DENSE = _env_float("RETRIEVAL_RRF_WEIGHT_DENSE", 0.4)

# ── 重排（见 app/rag/rerank.py）──────────────────────────────────────────────
# lexical（默认）：查询-切片词元重叠加权，无外部依赖；
# api：Cross-Encoder 重排服务（阿里百炼 qwen3.7-text-rerank / 硅基流动 BAAI/
#      bge-reranker-v2-m3 / Cohere 兼容协议），配置后自动启用，调用失败回退 lexical。
#      DashScope/maas 端点自动适配 /reranks（OpenAI 兼容）与原生 text-rerank 两种 schema。
RERANK_PROVIDER = os.environ.get("RERANK_PROVIDER", "").strip().lower()  # "" | lexical | api
RERANK_BASE_URL = os.environ.get("RERANK_BASE_URL", "")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "")  # 如 qwen3.7-text-rerank / BAAI/bge-reranker-v2-m3
# 未单独配置重排 Key 时回退 embedding Key（同账号场景免重复配置）
RERANK_API_KEY = os.environ.get("RERANK_API_KEY", "").strip() or os.environ.get("EMBEDDING_API_KEY", "").strip()
RERANK_TIMEOUT_SECONDS = _env_float("RERANK_TIMEOUT_SECONDS", 10.0)
# 显式指定重排 API 路径（默认空 = 按 base_url 自动探测 /reranks | 原生 text-rerank | /rerank）
RERANK_API_PATH = os.environ.get("RERANK_API_PATH", "").strip()
# 词元重叠加分步长与封顶（lexical 重排）
LEXICAL_RERANK_BOOST = _env_float("RETRIEVAL_LEXICAL_BOOST", 0.05)
LEXICAL_RERANK_CAP = _env_float("RETRIEVAL_LEXICAL_CAP", 0.25)
# 相关性阈值：>0 时仅对 API 重排的绝对相关分生效（低于阈值的证据丢弃，corrective-RAG 思路）
RELEVANCE_THRESHOLD = _env_float("RETRIEVAL_RELEVANCE_THRESHOLD", 0.0)

# ── 限流/超时重试的退避间隔（秒），运维可按供应商限流强度调整（评审 M-M7-1）──
EMBED_RETRY_SECONDS = _env_float("RETRIEVAL_EMBED_RETRY_SECONDS", 2.0)
COLLECT_OP_RETRY_BASE_SECONDS = _env_float("RETRIEVAL_RETRY_BASE_SECONDS", 8.0)

# HyDE（优化⑥）：语义类查询由 LLM 生成假设性证据文本再走稠密召回（仅稠密路，
# +1 次 LLM 调用/查询）。默认关闭；对模糊口语查询占比高的部署可开启。
HYDE_ENABLED = os.environ.get("RETRIEVAL_HYDE", "").strip().lower() in ("1", "true", "on", "yes")

# ── 流水线运行日志（进程内环形缓冲，供管理后台可视化）────────────────────────
RAG_RUN_LOG_SIZE = _env_int("RAG_RUN_LOG_SIZE", 50)
