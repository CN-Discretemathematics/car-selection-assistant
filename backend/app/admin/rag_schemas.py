"""RAG 管理接口响应模型（请求与响应必须使用 Pydantic Schema）。

轻量契约：静态字段强类型；流水线阶段的动态 detail、评测报告等演进型载荷用
`dict`（避免随节点细节变更频繁改版）。内容不含任何密钥（见 app/rag/service.get_status）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ── 车系款型覆盖率 ──────────────────────────────────────────────────────────
class CoverageOut(BaseModel):
    """车系款型覆盖率。

    「真缺口」与「全部停售」**分开计**——后者是上游标记的真实状态，
    详情页显示「暂无在售款型数据」本就正确，**不是回补目标**。
    混在一起会让回补指标永不收敛（2026-10-20 复核纠正）。
    """

    #: 在售车系（车系与品牌都 active）
    series_active: int
    #: 有**至少一个款型**（不论是否在售）的车系
    series_with_variants: int
    #: 有**在售款型**的车系
    series_on_sale: int
    #: **真缺口**：完全没有款型 → 回补目标
    gap_no_variants: int
    #: 有款型但**全部停售** → 数据完整，不是缺口
    gap_only_off_sale: int


# ── 流程图 ────────────────────────────────────────────────────────────────
class GraphNodeOut(BaseModel):
    id: str


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    conditional: bool = False


class GraphSpecOut(BaseModel):
    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)
    mermaid: str = ""


class RagGraphOut(BaseModel):
    query: GraphSpecOut
    ingest: GraphSpecOut


# ── 运行状态 ───────────────────────────────────────────────────────────────
class SparseStatusOut(BaseModel):
    backend: str
    chunks: int
    by_kind: dict[str, int] = Field(default_factory=dict)
    built_at: str | None = None


class DenseStatusOut(BaseModel):
    enabled: bool = False
    backend: str | None = None
    collection: str | None = None
    dim: int | None = None
    embedding_model: str | None = None
    error: str | None = None
    # 构建水位（.tmp/dense-build-meta.json）+ 与库内销量月份的滞后判定（2026-09 新增）
    built_at: str | None = None
    chunks: int | None = None
    sales_month: int | None = None
    db_sales_month: int | None = None
    stale: bool | None = None
    stale_reason: str | None = None


class RerankerStatusOut(BaseModel):
    provider: str
    active: str
    model: str | None = None


class StrategyOut(BaseModel):
    chunk_size: int
    chunk_overlap: int
    recall_multiplier: int
    rrf_k: int
    max_chunks: int


class RagStatusOut(BaseModel):
    backend_mode: str
    sparse: SparseStatusOut
    dense: DenseStatusOut
    reranker: RerankerStatusOut
    strategy: StrategyOut
    runs_logged: int
    db_counts: dict[str, int | None] | None = None
    #: 车系款型覆盖率。**独立于 db_counts**——db_counts 是索引重建的触发条件，
    #: 而覆盖率会随回补单调下降，并进去会让「回补一批款型」触发全量索引重建。
    coverage: CoverageOut | None = None


# ── 运行轨迹 / 试运行 ──────────────────────────────────────────────────────
class StageTraceOut(BaseModel):
    node: str
    duration_ms: float
    input_count: int
    output_count: int
    detail: dict | None = None


class RunRecordOut(BaseModel):
    ts: str
    query: str
    filters: dict = Field(default_factory=dict)
    top_k: int
    duration_ms: float
    result_count: int
    resolved_series: list[str] = Field(default_factory=list)
    stages: list[StageTraceOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dense: bool = False


class RagResultOut(BaseModel):
    chunk_id: str
    score: float
    text: str
    kind: str
    brand_id: int | None = None
    series_id: int | None = None
    variant_id: int | None = None
    source_id: int | None = None
    source_url: str | None = None


class RunsOut(BaseModel):
    runs: list[RunRecordOut] = Field(default_factory=list)


class TryQueryOut(BaseModel):
    run: RunRecordOut
    results: list[RagResultOut] = Field(default_factory=list)


# ── 索引重建 / 评测报告 ────────────────────────────────────────────────────
class ReindexSummaryOut(BaseModel):
    target: str
    chunks: int
    indexed: int
    by_kind: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ReindexOut(BaseModel):
    mode: str
    summary: ReindexSummaryOut | None = None


class ReindexProgressOut(BaseModel):
    running: bool = False
    target: str | None = None
    progress: dict[str, int] | None = None
    summary: ReindexSummaryOut | None = None
    error: str | None = None


class RagEvalOut(BaseModel):
    rag: dict | None = None
    agent: dict | None = None
