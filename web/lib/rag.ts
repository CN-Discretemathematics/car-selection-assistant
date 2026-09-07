/** RAG 流程管理 API（后端 /api/v1/admin/rag/*，管理凭据 Bearer token）。 */

const TOKEN_KEY = "***";

export function getAdminToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setAdminToken(token: string | null) {
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api/v1/admin/rag${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init.headers ?? {}),
    },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error((body && body.detail) ?? `请求失败（${res.status}）`);
  return body as T;
}

export interface GraphEdge {
  source: string;
  target: string;
  conditional: boolean;
}

export interface GraphSpec {
  nodes: { id: string }[];
  edges: GraphEdge[];
  mermaid: string;
}

export interface RagGraphs {
  query: GraphSpec;
  ingest: GraphSpec;
}

export interface StageTrace {
  node: string;
  duration_ms: number;
  input_count: number;
  output_count: number;
  detail: Record<string, unknown>;
}

export interface RunRecord {
  ts: string;
  query: string;
  filters: Record<string, unknown>;
  top_k: number;
  duration_ms: number;
  result_count: number;
  resolved_series: string[];
  stages: StageTrace[];
  warnings: string[];
  dense: boolean;
}

export interface RagResult {
  chunk_id: string;
  score: number;
  text: string;
  kind: string;
  brand_id: number | null;
  series_id: number | null;
  variant_id: number | null;
  source_id: number | null;
  source_url: string | null;
}

export interface RagStatus {
  backend_mode: string;
  sparse: {
    backend: string;
    chunks: number;
    by_kind: Record<string, number>;
    built_at: string | null;
  };
  dense: {
    enabled: boolean;
    backend: string | null;
    collection: string | null;
    dim: number | null;
    embedding_model: string | null;
    error: string | null;
  };
  reranker: { provider: string; active: string; model: string | null };
  strategy: {
    chunk_size: number;
    chunk_overlap: number;
    recall_multiplier: number;
    rrf_k: number;
    max_chunks: number;
  };
  runs_logged: number;
  db_counts?: Record<string, number>;
}

export interface TryQueryOut {
  run: RunRecord;
  results: RagResult[];
}

export interface ReindexProgress {
  running: boolean;
  target: string | null;
  progress: { done: number; total: number } | null;
  summary: { target: string; chunks: number; indexed: number; by_kind: Record<string, number>; warnings: string[] } | null;
  error: string | null;
}

export interface RagEvalStrategy {
  strategy: string;
  questions?: number;
  elapsed_seconds?: number;
  "@5"?: Record<string, number>;
  "@10"?: Record<string, number>;
  error?: string;
  [key: string]: unknown;
}

export interface RagEval {
  rag: {
    generated_at?: string;
    top_k?: number;
    backend_mode?: string;
    with_dense?: boolean;
    chunking?: Record<string, unknown>;
    strategies?: RagEvalStrategy[];
  } | null;
  agent: { pass_rate?: number; retrieval_hit_at_k?: number; total?: number } | null;
}

export const fetchRagGraph = (token: string) => request<RagGraphs>("/graph", token);
export const fetchRagStatus = (token: string) => request<RagStatus>("/status", token);
export const fetchRagRuns = (token: string, limit = 20) =>
  request<{ runs: RunRecord[] }>(`/runs?limit=${limit}`, token);
export const fetchRagEval = (token: string) => request<RagEval>("/eval", token);
export const fetchReindexProgress = (token: string) => request<ReindexProgress>("/reindex-progress", token);

export function runTryQuery(
  token: string,
  query: string,
  topK: number,
  filters?: Record<string, unknown>
): Promise<TryQueryOut> {
  return request<TryQueryOut>("/query", token, {
    method: "POST",
    body: JSON.stringify({ query, top_k: topK, filters: filters ?? null }),
  });
}

export function runReindex(
  token: string,
  target: "sparse" | "dense"
): Promise<{ mode: string; summary: ReindexProgress["summary"] }> {
  return request("/reindex", token, { method: "POST", body: JSON.stringify({ target }) });
}

/** 节点中文标签（流程图渲染）。 */
export const NODE_LABELS: Record<string, string> = {
  __start__: "开始",
  __end__: "结束",
  analyze: "查询理解",
  recall_sparse: "稀疏召回 BM25",
  recall_dense: "稠密召回 向量",
  fuse: "RRF 融合",
  rerank: "重排",
  grade: "证据把关",
  load: "数据装载",
  chunk: "切片构建",
  index: "索引写入",
};

export const KIND_LABELS: Record<string, string> = {
  series_intro: "系列介绍",
  series_summary: "车系摘要",
  spec_fact: "SKU 事实",
  source_document: "来源文档",
};
