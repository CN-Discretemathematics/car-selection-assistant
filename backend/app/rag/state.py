"""LangGraph 状态定义与阶段追踪。

RagState 在进程内传递（无 checkpointer 序列化需求），db 会话直接入状态。
并行召回分支各自只写自己的键；stages/warnings 用 operator.add reducer 归并，
每个节点通过 make_stage 记录耗时与输入/输出数量，供管理后台可视化与评测复盘。
"""
from __future__ import annotations

import operator
import time
from typing import Annotated, Any, TypedDict

from app.retrieval.backends import SearchResult


class StageTrace(TypedDict):
    node: str
    duration_ms: float
    input_count: int
    output_count: int
    detail: dict


class RagState(TypedDict, total=False):
    """查询流水线状态（pipeline.py）。"""

    db: Any  # sqlalchemy Session（进程内传递，不做序列化）
    query: str
    filters: dict | None
    top_k: int
    recall_k: int
    # analyze 输出：实体增强查询 + 归一化过滤条件
    entity_query: str
    resolved_series: list[str]
    # 多路召回与融合（sparse/dense 两分支并行，各写各键）
    sparse_hits: list[SearchResult]
    dense_hits: list[SearchResult]
    fused: list[SearchResult]
    reranked: list[SearchResult]
    reranker_absolute: bool  # 重排分是否为绝对相关分（Cross-Encoder），grade 阈值只对绝对分生效
    results: list[SearchResult]
    # 追踪（reducer 归并并行分支的写入）
    stages: Annotated[list[StageTrace], operator.add]
    warnings: Annotated[list[str], operator.add]


class IngestState(TypedDict, total=False):
    """摄取流水线状态（ingest.py）。"""

    db: Any
    limit: int
    materials: dict  # load 节点装载的原料（ORM 行/聚合结果，进程内传递）
    chunks: list[Any]  # list[SearchChunk]
    target: str  # sparse | dense | ""（仅构建切片不写索引）
    on_progress: Any  # Callable[[int, int], None]，dense 灌库进度回调
    indexed: int
    stages: Annotated[list[StageTrace], operator.add]
    warnings: Annotated[list[str], operator.add]


def make_stage(
    node: str,
    input_count: int,
    output_count: int,
    started: float,
    detail: dict | None = None,
) -> StageTrace:
    """构造一条节点执行轨迹（节点把它放进返回的 stages 增量里）。"""
    return StageTrace(
        node=node,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        input_count=input_count,
        output_count=output_count,
        detail=detail or {},
    )
