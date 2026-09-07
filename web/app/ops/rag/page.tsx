"use client";

/**
 * RAG 流程管理可视化。
 *
 * 数据源：后端 /api/v1/admin/rag/*（管理凭据）。功能：
 * - 流程图：LangGraph 两条流水线（查询/摄取）节点图 + mermaid 源码（可贴入 LangGraph Studio / 文档）；
 * - 运行状态：后端/索引/策略配置，一键重建索引（dense 后台任务 + 进度轮询）；
 * - 试运行：单条查询的阶段瀑布（耗时/输入输出/详情）+ 命中证据；
 * - 运行轨迹：最近查询流水线的运行记录；
 * - 评测报告：tools/eval_rag.py 的策略指标对比。
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import SiteHeader from "@/app/components/SiteHeader";
import {
  fetchRagEval,
  fetchRagGraph,
  fetchRagRuns,
  fetchRagStatus,
  fetchReindexProgress,
  getAdminToken,
  KIND_LABELS,
  NODE_LABELS,
  runReindex,
  runTryQuery,
  setAdminToken,
  type GraphSpec,
  type RagEval,
  type RagGraphs,
  type RagResult,
  type RagStatus,
  type RunRecord,
  type StageTrace,
} from "@/lib/rag";

type Tab = "graph" | "status" | "try" | "runs" | "eval";

const TABS: { id: Tab; label: string }[] = [
  { id: "graph", label: "流程图" },
  { id: "status", label: "运行状态" },
  { id: "try", label: "试运行" },
  { id: "runs", label: "运行轨迹" },
  { id: "eval", label: "评测报告" },
];

export default function OpsRagPage() {
  const [token, setToken] = useState<string | null>(null);
  const [tokenInput, setTokenInput] = useState("");
  const [tab, setTab] = useState<Tab>("graph");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setToken(getAdminToken());
  }, []);

  function saveToken() {
    const value = tokenInput.trim();
    if (!value) return;
    setAdminToken(value);
    setToken(value);
    setTokenInput("");
    setError(null);
  }

  function logout() {
    setAdminToken(null);
    setToken(null);
  }

  return (
    <div>
      <SiteHeader />
      <main className="mx-auto max-w-6xl px-4 py-8">
        <div className="animate-fade-up flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-3xl font-bold tracking-tight text-gray-900">
              <span className="text-gradient">RAG 流程管理</span>
            </h1>
            <p className="mt-2 text-sm text-gray-500">
              LangGraph 查询/摄取流水线可视化：召回 → 融合 → 重排 → 把关，索引重建与策略评测。
            </p>
          </div>
          {token && (
            <button onClick={logout} className="rounded-full border border-gray-200 px-3 py-1.5 text-xs text-gray-500 hover:bg-gray-50">
              退出管理
            </button>
          )}
        </div>

        {!token ? (
          <div className="mx-auto mt-10 max-w-md rounded-2xl border border-gray-200/80 bg-white p-6 shadow-sm">
            <p className="text-sm font-semibold text-gray-900">输入管理凭据</p>
            <p className="mt-1 text-xs text-gray-500">
              即后端 ADMIN_API_TOKEN（经 KMS 注入的独立凭据，不开放注册）。
            </p>
            <div className="mt-4 flex gap-2">
              <input
                type="password"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && saveToken()}
                placeholder="Bearer token"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-blue-400"
              />
              <button onClick={saveToken} className="rounded-xl bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700">
                进入
              </button>
            </div>
          </div>
        ) : (
          <>
            <nav className="mt-6 flex flex-wrap gap-1 text-sm">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setTab(t.id)}
                  className={`rounded-full px-3.5 py-1.5 transition-colors ${
                    tab === t.id ? "bg-blue-50 font-semibold text-blue-700" : "text-gray-600 hover:bg-gray-100"
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </nav>
            {error && (
              <p className="mt-4 rounded-xl border border-red-100 bg-red-50 px-4 py-2 text-sm text-red-600">{error}</p>
            )}
            <div className="mt-6">
              {tab === "graph" && <GraphTab token={token} onError={setError} />}
              {tab === "status" && <StatusTab token={token} onError={setError} />}
              {tab === "try" && <TryTab token={token} onError={setError} />}
              {tab === "runs" && <RunsTab token={token} onError={setError} />}
              {tab === "eval" && <EvalTab token={token} onError={setError} />}
            </div>
          </>
        )}
      </main>
    </div>
  );
}

interface TabProps {
  token: string;
  onError: (msg: string | null) => void;
}

/* ── 流程图 ─────────────────────────────────────────────────────────────── */

function longestPathRanks(spec: GraphSpec): Map<string, number> {
  const ranks = new Map<string, number>();
  const incoming = new Map<string, string[]>();
  for (const e of spec.edges) {
    incoming.set(e.target, [...(incoming.get(e.target) ?? []), e.source]);
  }
  const visiting = new Set<string>();
  function rankOf(id: string): number {
    const cached = ranks.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0; // 环保护（当前两图均为 DAG）
    visiting.add(id);
    const preds = incoming.get(id) ?? [];
    const r = preds.length === 0 ? 0 : Math.max(...preds.map((p) => rankOf(p) + 1));
    visiting.delete(id);
    ranks.set(id, r);
    return r;
  }
  for (const n of spec.nodes) rankOf(n.id);
  return ranks;
}

function FlowDiagram({ spec }: { spec: GraphSpec }) {
  const ranks = longestPathRanks(spec);
  const rows = new Map<number, string[]>();
  for (const n of spec.nodes) {
    const r = ranks.get(n.id) ?? 0;
    rows.set(r, [...(rows.get(r) ?? []), n.id]);
  }
  const sortedRanks = [...rows.keys()].sort((a, b) => a - b);
  const conditionalTargets = new Set(spec.edges.filter((e) => e.conditional).map((e) => e.target));

  return (
    <div className="rounded-2xl border border-gray-200/80 bg-white p-6 shadow-sm">
      <div className="flex flex-col items-center gap-1">
        {sortedRanks.map((r, i) => (
          <div key={r} className="flex flex-col items-center">
            {i > 0 && (
              <div className={`my-1 text-lg leading-none ${rows.get(r)?.some((id) => conditionalTargets.has(id)) ? "text-amber-500" : "text-gray-300"}`}>
                ↓{rows.get(r)?.some((id) => conditionalTargets.has(id)) ? <span className="ml-1 align-middle text-[10px]">条件</span> : null}
              </div>
            )}
            <div className="flex flex-wrap items-center justify-center gap-3">
              {(rows.get(r) ?? []).map((id) => {
                const terminal = id === "__start__" || id === "__end__";
                return (
                  <div
                    key={id}
                    className={`rounded-xl border px-4 py-2 text-center text-sm shadow-sm ${
                      terminal
                        ? "border-gray-200 bg-gray-50 text-gray-500"
                        : "border-blue-200 bg-gradient-to-br from-blue-50 to-indigo-50 font-semibold text-blue-800"
                    }`}
                  >
                    {NODE_LABELS[id] ?? id}
                    <span className="mt-0.5 block font-mono text-[10px] font-normal text-gray-400">{id}</span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <div className="mt-5 flex flex-wrap gap-1.5">
        {spec.edges.map((e, i) => (
          <span
            key={i}
            className={`rounded-full px-2.5 py-1 font-mono text-[10px] ${
              e.conditional ? "bg-amber-50 text-amber-700" : "bg-gray-50 text-gray-500"
            }`}
          >
            {e.source} → {e.target}
            {e.conditional ? "（条件）" : ""}
          </span>
        ))}
      </div>
      <details className="mt-4">
        <summary className="cursor-pointer text-xs text-gray-400 hover:text-gray-600">
          mermaid 源码（LangGraph 自动生成，可贴入文档 / LangGraph Studio）
        </summary>
        <pre className="mt-2 max-h-72 overflow-auto rounded-xl bg-gray-900 p-4 text-[11px] leading-relaxed text-gray-100">
          {spec.mermaid}
        </pre>
      </details>
    </div>
  );
}

function GraphTab({ token, onError }: TabProps) {
  const [graphs, setGraphs] = useState<RagGraphs | null>(null);

  useEffect(() => {
    fetchRagGraph(token).then(setGraphs).catch((e) => onError(e instanceof Error ? e.message : "加载失败"));
  }, [token, onError]);

  if (!graphs) return <p className="text-center text-gray-400">加载中…</p>;
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <section>
        <h2 className="mb-3 text-sm font-semibold text-gray-700">查询流水线（在线检索）</h2>
        <FlowDiagram spec={graphs.query} />
      </section>
      <section>
        <h2 className="mb-3 text-sm font-semibold text-gray-700">摄取流水线（离线索引）</h2>
        <FlowDiagram spec={graphs.ingest} />
      </section>
    </div>
  );
}

/* ── 运行状态 ───────────────────────────────────────────────────────────── */

function StatusCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-2xl border border-gray-200/80 bg-white p-5 shadow-sm">
      <p className="text-xs font-semibold uppercase tracking-wide text-gray-400">{title}</p>
      <div className="mt-2 text-sm text-gray-800">{children}</div>
    </div>
  );
}

function StatusTab({ token, onError }: TabProps) {
  const [status, setStatus] = useState<RagStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(() => {
    fetchRagStatus(token).then(setStatus).catch((e) => onError(e instanceof Error ? e.message : "加载失败"));
  }, [token, onError]);

  useEffect(load, [load]);
  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  async function reindex(target: "sparse" | "dense") {
    setBusy(target);
    setNotice(null);
    try {
      const out = await runReindex(token, target);
      if (out.mode === "sync") {
        const s = out.summary;
        setNotice(`稀疏索引重建完成：切片 ${s?.chunks ?? 0}，写入 ${s?.indexed ?? 0}。`);
        load();
      } else {
        setNotice("dense 重建任务已启动（embedding + 上传，可能耗时数分钟）…");
        pollRef.current = window.setInterval(async () => {
          try {
            const p = await fetchReindexProgress(token);
            setProgress(p.progress);
            if (!p.running) {
              if (pollRef.current) window.clearInterval(pollRef.current);
              pollRef.current = null;
              setBusy(null);
              setProgress(null);
              setNotice(p.error ? `dense 重建失败：${p.error}` : `dense 重建完成：写入 ${p.summary?.indexed ?? 0} 条。`);
              load();
            }
          } catch {
            /* 轮询瞬态失败忽略 */
          }
        }, 2500);
        return;
      }
    } catch (e) {
      onError(e instanceof Error ? e.message : "重建失败");
    }
    setBusy(null);
  }

  if (!status) return <p className="text-center text-gray-400">加载中…</p>;
  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        <StatusCard title="后端模式">
          <p className="font-mono text-lg font-bold">{status.backend_mode}</p>
          <p className="mt-1 text-xs text-gray-400">inmemory=纯稀疏（开发）；milvus=稀疏∥稠密混合（生产）</p>
        </StatusCard>
        <StatusCard title="稀疏索引（BM25）">
          <p className="text-lg font-bold">{status.sparse.chunks.toLocaleString()} 切片</p>
          <div className="mt-1 flex flex-wrap gap-1">
            {Object.entries(status.sparse.by_kind).map(([k, v]) => (
              <span key={k} className="rounded-full bg-gray-50 px-2 py-0.5 text-[11px] text-gray-500">
                {KIND_LABELS[k] ?? k} {v}
              </span>
            ))}
          </div>
          <p className="mt-1 text-xs text-gray-400">构建于 {status.sparse.built_at ?? "未构建"}</p>
        </StatusCard>
        <StatusCard title="稠密索引（向量）">
          {status.dense.enabled ? (
            <>
              <p className="font-semibold text-emerald-600">已启用</p>
              <p className="mt-1 text-xs text-gray-500">
                集合 {status.dense.collection} · {status.dense.dim} 维 · embedding {status.dense.embedding_model ?? "未配置"}
              </p>
            </>
          ) : (
            <>
              <p className="font-semibold text-gray-400">未启用</p>
              <p className="mt-1 text-xs text-gray-400">{status.dense.error ?? "RETRIEVAL_BACKEND=inmemory（开发模式）"}</p>
            </>
          )}
        </StatusCard>
        <StatusCard title="重排器">
          <p className="font-semibold">{status.reranker.active === "cross_encoder" ? `Cross-Encoder（${status.reranker.model}）` : "词元重叠 lexical"}</p>
          <p className="mt-1 text-xs text-gray-400">配置 RERANK_PROVIDER={status.reranker.provider || "（空）"}</p>
        </StatusCard>
        <StatusCard title="策略参数">
          <ul className="space-y-0.5 text-xs text-gray-600">
            <li>切分：{status.strategy.chunk_size} 字符 / 重叠 {status.strategy.chunk_overlap}</li>
            <li>召回：每路 top_k × {status.strategy.recall_multiplier}（上限 {status.strategy.max_chunks.toLocaleString()} 切片）</li>
            <li>融合：RRF k={status.strategy.rrf_k}</li>
          </ul>
        </StatusCard>
        <StatusCard title="数据库规模">
          {status.db_counts ? (
            <ul className="space-y-0.5 text-xs text-gray-600">
              {Object.entries(status.db_counts).map(([k, v]) => (
                <li key={k}>{k}: {v.toLocaleString()}</li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-gray-400">不可用</p>
          )}
        </StatusCard>
      </div>

      <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-gray-200/80 bg-white p-5 shadow-sm">
        <button
          onClick={() => reindex("sparse")}
          disabled={busy !== null}
          className="rounded-xl bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {busy === "sparse" ? "重建中…" : "重建稀疏索引"}
        </button>
        <button
          onClick={() => reindex("dense")}
          disabled={busy !== null || !status.dense.enabled}
          className="rounded-xl border border-blue-200 px-4 py-2 text-sm font-semibold text-blue-700 hover:bg-blue-50 disabled:opacity-40"
        >
          {busy === "dense" ? "灌库中…" : "重建稠密索引（Zilliz）"}
        </button>
        {progress && (
          <span className="text-xs text-gray-500">
            进度 {progress.done.toLocaleString()}/{progress.total.toLocaleString()}（{progress.total ? Math.round((progress.done / progress.total) * 100) : 0}%）
          </span>
        )}
        {notice && <span className="text-xs text-gray-500">{notice}</span>}
      </div>
    </div>
  );
}

/* ── 阶段瀑布 ───────────────────────────────────────────────────────────── */

function StageWaterfall({ stages }: { stages: StageTrace[] }) {
  const maxMs = Math.max(...stages.map((s) => s.duration_ms), 1);
  return (
    <div className="space-y-1.5">
      {stages.map((s, i) => (
        <details key={i} className="group rounded-xl border border-gray-100 bg-gray-50/50 px-3 py-2">
          <summary className="flex cursor-pointer list-none items-center gap-3 text-xs">
            <span className="w-28 shrink-0 font-semibold text-gray-700">{NODE_LABELS[s.node] ?? s.node}</span>
            <span className="h-2 flex-1 overflow-hidden rounded-full bg-gray-200/70">
              <span
                className="block h-full rounded-full bg-gradient-to-r from-blue-500 to-indigo-500"
                style={{ width: `${Math.max((s.duration_ms / maxMs) * 100, 2)}%` }}
              />
            </span>
            <span className="w-16 shrink-0 text-right font-mono text-gray-500">{s.duration_ms} ms</span>
            <span className="w-20 shrink-0 text-right font-mono text-gray-400">
              {s.input_count} → {s.output_count}
            </span>
          </summary>
          <pre className="mt-2 overflow-auto rounded-lg bg-white p-2 font-mono text-[11px] text-gray-500">
            {JSON.stringify(s.detail, null, 1)}
          </pre>
        </details>
      ))}
    </div>
  );
}

function ResultCards({ results }: { results: RagResult[] }) {
  if (results.length === 0) return <p className="text-sm text-gray-400">无命中结果。</p>;
  return (
    <div className="space-y-2">
      {results.map((r) => (
        <div key={r.chunk_id} className="rounded-xl border border-gray-200/80 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="rounded-full bg-blue-50 px-2 py-0.5 font-semibold text-blue-700">{KIND_LABELS[r.kind] ?? r.kind}</span>
            <span className="font-mono text-gray-400">{r.chunk_id}</span>
            <span className="font-mono text-gray-500">score {r.score}</span>
            {r.series_id != null && <span className="text-gray-400">series {r.series_id}</span>}
            {r.source_url && (
              <a href={r.source_url} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline">
                来源 ↗
              </a>
            )}
          </div>
          <p className="mt-2 text-sm leading-relaxed text-gray-700">{r.text}</p>
        </div>
      ))}
    </div>
  );
}

/* ── 试运行 ─────────────────────────────────────────────────────────────── */

function TryTab({ token, onError }: TabProps) {
  const [query, setQuery] = useState("家用SUV 大空间 五座");
  const [topK, setTopK] = useState(5);
  const [seriesId, setSeriesId] = useState("");
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<{ run: RunRecord; results: RagResult[] } | null>(null);

  async function run() {
    setBusy(true);
    try {
      const filters = seriesId.trim() ? { series_id: Number(seriesId.trim()) } : undefined;
      setOut(await runTryQuery(token, query, topK, filters));
      onError(null);
    } catch (e) {
      onError(e instanceof Error ? e.message : "试运行失败");
    }
    setBusy(false);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-gray-200/80 bg-white p-5 shadow-sm">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && run()}
          placeholder="输入查询，如：腾势Z9GT 续航怎么样"
          className="min-w-64 flex-1 rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-blue-400"
        />
        <label className="flex items-center gap-1.5 text-xs text-gray-500">
          top_k
          <select
            value={topK}
            onChange={(e) => setTopK(Number(e.target.value))}
            className="rounded-lg border border-gray-200 px-2 py-1.5 text-sm"
          >
            {[3, 5, 8, 10].map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-gray-500">
          series_id 过滤
          <input
            value={seriesId}
            onChange={(e) => setSeriesId(e.target.value.replace(/[^\d]/g, ""))}
            placeholder="可选"
            className="w-24 rounded-lg border border-gray-200 px-2 py-1.5 text-sm outline-none focus:border-blue-400"
          />
        </label>
        <button
          onClick={run}
          disabled={busy || !query.trim()}
          className="rounded-xl bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {busy ? "运行中…" : "运行流水线"}
        </button>
      </div>

      {out && (
        <div className="grid gap-4 lg:grid-cols-2">
          <section className="rounded-2xl border border-gray-200/80 bg-white p-5 shadow-sm">
            <div className="flex items-baseline justify-between">
              <h3 className="text-sm font-semibold text-gray-700">阶段瀑布</h3>
              <span className="font-mono text-xs text-gray-400">总耗时 {out.run.duration_ms} ms</span>
            </div>
            {out.run.resolved_series.length > 0 && (
              <p className="mt-2 text-xs text-gray-500">
                实体解析：<span className="font-semibold text-blue-700">{out.run.resolved_series.join("、")}</span>
              </p>
            )}
            {out.run.warnings.length > 0 && (
              <ul className="mt-2 space-y-1">
                {out.run.warnings.map((w, i) => (
                  <li key={i} className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-xs text-amber-700">⚠ {w}</li>
                ))}
              </ul>
            )}
            <div className="mt-3">
              <StageWaterfall stages={out.run.stages} />
            </div>
          </section>
          <section>
            <h3 className="mb-3 text-sm font-semibold text-gray-700">命中证据（{out.results.length}）</h3>
            <ResultCards results={out.results} />
          </section>
        </div>
      )}
    </div>
  );
}

/* ── 运行轨迹 ───────────────────────────────────────────────────────────── */

function RunsTab({ token, onError }: TabProps) {
  const [runs, setRuns] = useState<RunRecord[] | null>(null);

  const load = useCallback(() => {
    fetchRagRuns(token).then((d) => setRuns(d.runs)).catch((e) => onError(e instanceof Error ? e.message : "加载失败"));
  }, [token, onError]);

  useEffect(load, [load]);

  if (runs === null) return <p className="text-center text-gray-400">加载中…</p>;
  if (runs.length === 0)
    return <p className="rounded-2xl border border-gray-200/80 bg-white p-8 text-center text-sm text-gray-400 shadow-sm">暂无运行记录（进程内最近 {50} 条；在「试运行」或 Agent 对话中发起查询后出现）。</p>;
  return (
    <div className="space-y-2">
      {runs.map((r, i) => (
        <details key={i} className="rounded-2xl border border-gray-200/80 bg-white p-4 shadow-sm">
          <summary className="flex cursor-pointer list-none flex-wrap items-center gap-3 text-sm">
            <span className="font-mono text-xs text-gray-400">{r.ts.replace("T", " ").replace("+00:00", "Z")}</span>
            <span className="max-w-md truncate font-medium text-gray-800">{r.query || "（空查询）"}</span>
            <span className="rounded-full bg-gray-50 px-2 py-0.5 text-[11px] text-gray-500">{r.result_count} 条结果</span>
            <span className="rounded-full bg-gray-50 px-2 py-0.5 font-mono text-[11px] text-gray-500">{r.duration_ms} ms</span>
            {r.dense && <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] text-indigo-600">dense</span>}
            {r.warnings.length > 0 && (
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] text-amber-600">{r.warnings.length} 警告</span>
            )}
          </summary>
          <div className="mt-3 space-y-3">
            {r.warnings.map((w, j) => (
              <p key={j} className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-xs text-amber-700">⚠ {w}</p>
            ))}
            <StageWaterfall stages={r.stages} />
          </div>
        </details>
      ))}
      <button onClick={load} className="rounded-full border border-gray-200 px-3 py-1.5 text-xs text-gray-500 hover:bg-gray-50">
        刷新
      </button>
    </div>
  );
}

/* ── 评测报告 ───────────────────────────────────────────────────────────── */

function EvalTab({ token, onError }: TabProps) {
  const [evalReport, setEvalReport] = useState<RagEval | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    fetchRagEval(token)
      .then((d) => { setEvalReport(d); setLoadError(null); onError(null); })
      .catch((e) => setLoadError(e instanceof Error ? e.message : "加载失败"));
  }, [token, onError]);

  if (loadError)
    return (
      <div className="rounded-2xl border border-gray-200/80 bg-white p-8 text-center shadow-sm">
        <p className="text-sm text-gray-500">{loadError}</p>
        <p className="mt-2 font-mono text-xs text-gray-400">python tools/eval_rag.py --limit 50</p>
      </div>
    );
  if (!evalReport) return <p className="text-center text-gray-400">加载中…</p>;

  const rag = evalReport.rag;
  const k = rag?.top_k ?? 5;
  return (
    <div className="space-y-4">
      {rag && (
        <section className="rounded-2xl border border-gray-200/80 bg-white p-5 shadow-sm">
          <h3 className="text-sm font-semibold text-gray-700">检索策略对比（tools/eval_rag.py · {rag.generated_at}）</h3>
          <p className="mt-1 text-xs text-gray-400">
            后端 {rag.backend_mode} · dense 参与：{String(rag.with_dense ?? false)} ·
            判定：问题 anchors（真实车系/SKU）
          </p>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-gray-100 text-gray-400">
                  <th className="py-2 pr-3 font-medium">策略</th>
                  <th className="py-2 pr-3 font-medium">Hit@{k}</th>
                  <th className="py-2 pr-3 font-medium">Recall@{k}</th>
                  <th className="py-2 pr-3 font-medium">P@{k}</th>
                  <th className="py-2 pr-3 font-medium">MRR</th>
                  <th className="py-2 pr-3 font-medium">NDCG@10</th>
                  <th className="py-2 font-medium">耗时s</th>
                </tr>
              </thead>
              <tbody>
                {(rag.strategies ?? []).map((s) => {
                  const mk = (s[`@${k}`] ?? {}) as Record<string, number>;
                  const m10 = (s["@10"] ?? {}) as Record<string, number>;
                  return (
                    <tr key={s.strategy} className="border-b border-gray-50">
                      <td className="py-2 pr-3 font-mono font-semibold text-gray-700">{s.strategy}</td>
                      {s.error ? (
                        <td colSpan={6} className="py-2 text-red-500">{s.error}</td>
                      ) : (
                        <>
                          <td className="py-2 pr-3 font-mono">{mk.hit ?? "-"}</td>
                          <td className="py-2 pr-3 font-mono">{mk.recall ?? "-"}</td>
                          <td className="py-2 pr-3 font-mono">{mk.precision ?? "-"}</td>
                          <td className="py-2 pr-3 font-mono">{mk.mrr ?? "-"}</td>
                          <td className="py-2 pr-3 font-mono">{m10.ndcg ?? "-"}</td>
                          <td className="py-2 font-mono text-gray-400">{s.elapsed_seconds ?? "-"}</td>
                        </>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {evalReport.agent && (
        <section className="rounded-2xl border border-gray-200/80 bg-white p-5 shadow-sm">
          <h3 className="text-sm font-semibold text-gray-700">Agent 端到端评测（tools/eval_agent.py）</h3>
          <p className="mt-2 text-xs text-gray-600">
            通过率 {((evalReport.agent.pass_rate ?? 0) * 100).toFixed(1)}% · 检索 hit@k{" "}
            {((evalReport.agent.retrieval_hit_at_k ?? 0) * 100).toFixed(1)}% · 共 {evalReport.agent.total ?? "-"} 题
          </p>
        </section>
      )}
    </div>
  );
}
