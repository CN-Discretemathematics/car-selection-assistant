"use client";

import Link from "next/link";
import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import HeroGlow from "@/app/components/HeroGlow";
import Reveal from "@/app/components/Reveal";
import RiseText from "@/app/components/RiseText";
import SiteHeader from "@/app/components/SiteHeader";
import { askAgent } from "@/lib/agentTriggers";
import {
  CATEGORY_LABELS,
  ENERGY_LABELS,
  formatPrice,
  type ComparisonDetail,
  type CompareVariantOut,
} from "@/lib/api";

const MISSING_LABEL = "官方资料未披露";
const MAX_COMPARE = 5;

function useComparison(): { data: ComparisonDetail | null; error: string | null } {
  const searchParams = useSearchParams();
  const [data, setData] = useState<ComparisonDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const raw = searchParams.get("variant_ids") ?? "";
    const ids = raw
      .split(",")
      .map((s) => Number(s))
      .filter((n) => Number.isInteger(n) && n > 0)
      .slice(0, MAX_COMPARE);

    if (ids.length === 0) {
      setData(null);
      setError(null);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const createdRes = await fetch("/api/v1/comparisons", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ variant_ids: ids }),
        });
        if (!createdRes.ok) {
          const body = await createdRes.json().catch(() => null);
          setError(body?.detail ?? `请求失败（${createdRes.status}）`);
          return;
        }
        const created = await createdRes.json();
        const detailRes = await fetch(`/api/v1/comparisons/${created.id}`, { cache: "no-store" });
        if (!detailRes.ok) throw new Error(`请求失败（${detailRes.status}）`);
        const detail = (await detailRes.json()) as ComparisonDetail;
        if (!cancelled) setData(detail);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "网络错误");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [searchParams]);

  return { data, error };
}

/** 差异分析结果（后端确定性推导：谁领先、差多少、贵在哪、缺什么数据）。 */
interface AnalysisValue {
  variant_id: number;
  display: string;
  raw: number | null;
  leader: boolean;
}
interface AnalysisDimension {
  key: string;
  label: string;
  why: string;
  values: AnalysisValue[];
  significant: boolean;
  gap: string | null;
  note: string | null;
}
interface ComparisonAnalysis {
  variants: { variant_id: number; label: string; price: number | null; leaders: string[]; trails: string[] }[];
  price: AnalysisDimension | null;
  dimensions: AnalysisDimension[];
  tradeoffs: string[];
  summary: string[];
  gaps: { dimension: string; missing: string[] }[];
  /** 一句话结论（后端确定性模板：谁强在哪 + 谁最便宜）。 */
  verdict: string | null;
  /** 关键差异 Top3（按差距百分比从大到小）。 */
  key_points: { label: string; winner: string; gap: string }[];
}

/** 取差异分析（对比页专用端点；与参数表并存，不是替代）。 */
function useAnalysis(variantIds: number[]): { analysis: ComparisonAnalysis | null; loading: boolean } {  const [analysis, setAnalysis] = useState<ComparisonAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const key = variantIds.join(",");

  useEffect(() => {
    if (variantIds.length < 2) {
      setAnalysis(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const res = await fetch("/api/v1/comparisons/analysis", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ variant_ids: variantIds }),
        });
        if (!res.ok) return; // 分析失败不影响参数表（页面仍可用）
        const body = (await res.json()) as ComparisonAnalysis;
        if (!cancelled) setAnalysis(body);
      } catch {
        /* 静默降级：参数表仍在 */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return { analysis, loading };
}

/** 取 LLM 一句话点评（独立端点，随后台补充；后端不可用/越界时返回 null，面板回退确定性结论）。 */
function useAiComment(variantIds: number[], enabled: boolean): { aiComment: string | null } {
  const [aiComment, setAiComment] = useState<string | null>(null);
  const key = variantIds.join(",");

  useEffect(() => {
    setAiComment(null);
    if (!enabled || variantIds.length < 2) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/v1/comparisons/analysis/ai-comment", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ variant_ids: variantIds }),
        });
        if (!res.ok) return; // 静默降级：点评缺失时面板仍显示确定性结论
        const body = (await res.json()) as { ai_comment: string | null };
        if (!cancelled && body.ai_comment) setAiComment(body.ai_comment);
      } catch {
        /* 静默降级 */
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled]);

  return { aiComment };
}

/** 差异分析面板：先给一句话结论与关键差异，细节默认折叠（缺数据如实标注）。 */
function AnalysisPanel({
  analysis,
  aiComment,
  names,
  trims,
}: {
  analysis: ComparisonAnalysis;
  aiComment: string | null;
  names: Map<number, string>;
  trims: Map<number, string>;
}) {
  const significant = analysis.dimensions.filter((d) => d.significant);
  const notes = analysis.dimensions.filter((d) => !d.significant && d.note);
  // 跳转高亮：✨按钮触发「dsh:flash-analysis」，短暂高亮面板让用户知道该看哪
  const [flash, setFlash] = useState(false);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const handler = () => {
      setFlash(true);
      window.setTimeout(() => setFlash(false), 1800);
    };
    window.addEventListener("dsh:flash-analysis", handler);
    return () => window.removeEventListener("dsh:flash-analysis", handler);
  }, []);
  return (
    <Reveal className="mt-6" delay={60}>
      <div
        id="analysis-panel"
        className={`glass nums rounded-[22px] border p-5 transition duration-500 sm:p-6 ${
          flash ? "border-apple/60 ring-2 ring-apple/25" : "border-apple/15"
        }`}
      >
        <h2 className="flex items-center gap-2 text-[18px] font-semibold tracking-tight text-ink">
          差异分析
          <span className="rounded-full bg-apple/10 px-2 py-0.5 text-[11px] font-semibold text-apple">
            基于库内参数自动比较
          </span>
        </h2>

        {aiComment && (
          <p className="mt-3 text-[15.5px] leading-7 text-ink">
            <span className="mr-2 inline-block rounded-full bg-apple/10 px-2 py-0.5 align-middle text-[11px] font-semibold text-apple">
              AI 点评
            </span>
            {aiComment}
          </p>
        )}

        {analysis.verdict && (
          <p className="mt-3 border-l-2 border-apple/40 pl-3 text-[13.5px] leading-6 text-ink-soft">
            {analysis.verdict}
          </p>
        )}

        {analysis.key_points.length > 0 && (
          <ul className="mt-3 space-y-1 text-[13.5px] leading-6">
            {analysis.key_points.map((p) => (
              <li key={p.label} className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium text-ink">{p.label}</span>
                <span className="text-ink-soft">
                  <span className="font-semibold text-apple">{p.winner}</span> 领先
                </span>
                <span className="text-ash">{p.gap}</span>
              </li>
            ))}
          </ul>
        )}

        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="press mt-3 text-[12.5px] font-medium text-apple underline-offset-4 hover:underline"
        >
          {expanded ? "收起明细" : `展开全部 ${significant.length} 项对比（含原因与缺数据说明）`}
        </button>

        {expanded && (
          <div className="mt-3 space-y-4 border-t border-black/[0.05] pt-3">
            <ul className="space-y-1.5 text-[13.5px] leading-6 text-ink-soft">
              {analysis.summary.map((line) => (
                <li key={line} className="flex gap-2">
                  <span className="text-apple">·</span>
                  <span>{line}</span>
                </li>
              ))}
            </ul>

            {significant.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] text-[13px]">
                  <thead>
                    <tr className="border-b border-black/[0.06] text-left text-ash">
                      <th className="py-2 font-medium">维度</th>
                      {analysis.variants.map((v) => (
                        <th key={v.variant_id} className="py-2 font-medium">
                          {/* 同车系多款型对比时，两列若只显示「品牌 车系」会完全一样 →
                              车系名做小字，款型名（含年款/配置）做正文，一眼分得清 */}
                          <span className="block text-[11px] font-normal text-ash">
                            {names.get(v.variant_id) ?? `款型 ${v.variant_id}`}
                          </span>
                          <span className="block font-medium text-ink">
                            {trims.get(v.variant_id) ?? ""}
                          </span>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {significant.map((dim) => (
                      <tr key={dim.key} className="border-b border-black/[0.04]">
                        <td className="py-2 pr-3">
                          <span className="font-medium text-ink">{dim.label}</span>
                          {dim.why && <span className="block text-[11px] text-ash">{dim.why}</span>}
                        </td>
                        {dim.values.map((val) => (
                          <td key={val.variant_id} className="py-2 pr-3">
                            <span className={val.leader ? "font-semibold text-apple" : "text-ink-soft"}>
                              {val.display}
                              {val.leader && " ★"}
                            </span>
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {analysis.tradeoffs.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-[13px] font-semibold text-ink">取舍</p>
                {analysis.tradeoffs.map((line) => (
                  <p key={line} className="text-[13px] leading-6 text-ink-soft">
                    {line}
                  </p>
                ))}
              </div>
            )}

            {(analysis.gaps.length > 0 || notes.length > 0) && (
              <p className="text-[12px] leading-5 text-ash">
                信息缺口：
                {analysis.gaps.map((g) => `${g.dimension}（${g.missing.length} 个款型无数据）`).join("、")}
                {notes.length > 0 && (analysis.gaps.length > 0 ? "；" : "") + notes.map((n) => `${n.label}：${n.note}`).join("；")}
              </p>
            )}
          </div>
        )}
      </div>
    </Reveal>
  );
}

interface FactRow {
  category: string;
  fact_key: string;
  label: string;
  display: string;
  values: (string | null)[];
}

function buildRows(variants: CompareVariantOut[]): FactRow[] {
  const map = new Map<string, FactRow>();
  for (let i = 0; i < variants.length; i++) {
    for (const f of variants[i].facts) {
      const key = `${f.category}|${f.fact_key}`;
      let row = map.get(key);
      if (!row) {
        row = { category: f.category, fact_key: f.fact_key, label: f.label || f.fact_key, display: f.display, values: Array(variants.length).fill(null) };
        map.set(key, row);
      }
      row.values[i] = f.display;
    }
  }
  return [...map.values()].sort((a, b) => a.category.localeCompare(b.category) || a.fact_key.localeCompare(b.fact_key));
}

function CompareTable({ data }: { data: ComparisonDetail }) {
  const [hideSame, setHideSame] = useState(true);
  const rows = useMemo(() => buildRows(data.variants), [data.variants]);
  const categories = useMemo(() => [...new Set(rows.map((r) => r.category))], [rows]);
  const commonKeys = useMemo(
    () => new Set(data.common_params.map((p) => `${p.category}|${p.fact_key}`)),
    [data.common_params],
  );
  // 参数分类多选（null=全部）；自动过滤已随数据集变化失效的旧选择
  const [picked, setPicked] = useState<string[] | null>(null);
  // 差异分析：与参数表并行取数（分析失败时静默降级，参数表照常可用）
  const { analysis, loading } = useAnalysis(data.variants.map((v) => v.variant_id));
  const { aiComment } = useAiComment(data.variants.map((v) => v.variant_id), analysis !== null);
  const activePicked = useMemo(() => {
    if (!picked) return null;
    const valid = picked.filter((c) => categories.includes(c));
    if (valid.length === 0 || valid.length === categories.length) return null;
    return valid;
  }, [picked, categories]);

  function toggleCategory(cat: string) {
    if (activePicked === null) {
      // 从「全部参数」点击某分类 → 只选中该类（而非反选其余，用户反馈修正）
      setPicked([cat]);
      return;
    }
    if (activePicked.includes(cat)) {
      const next = activePicked.filter((c) => c !== cat);
      setPicked(next.length === 0 ? null : next); // 取消至空集 → 回到「全部参数」
    } else {
      setPicked([...activePicked, cat]);
    }
  }

  const visibleRows = rows.filter(
    (r) =>
      (!activePicked || activePicked.includes(r.category)) &&
      (!hideSame || !commonKeys.has(`${r.category}|${r.fact_key}`)),
  );

  const share = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      window.alert("对比链接已复制，可直接分享。");
    } catch {
      window.alert("复制失败，请手动复制地址栏链接。");
    }
  };

  return (
    <div>
      <Reveal className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <button
          type="button"
          onClick={() => setHideSame((v) => !v)}
          className="glass press rounded-full border border-black/[0.05] px-4 py-2 text-sm text-ink-soft hover:border-apple/30 hover:text-apple"
        >
          {hideSame ? "显示相同参数" : "隐藏相同参数"}（{commonKeys.size} 项相同）
        </button>
        <button
          type="button"
          onClick={share}
          className="press rounded-full border border-apple/25 bg-ice/80 px-4 py-2 text-sm font-medium text-apple hover:bg-ice"
        >
          复制分享链接
        </button>
      </Reveal>

      {/* 对比项目筛选：按参数分类选择想对比的具体项目 */}
      <Reveal className="glass mb-5 rounded-[22px] border border-black/[0.05] p-3.5" delay={60}>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-xs font-semibold text-ash">对比项目</span>
          <button
            type="button"
            onClick={() => setPicked(null)}
            className={`press rounded-full border px-3.5 py-1.5 text-xs duration-300 ${
              activePicked === null
                ? "border-apple bg-apple font-medium text-white shadow-sm shadow-apple/30"
                : "border-black/[0.08] bg-white/85 text-ink-soft hover:border-apple/35 hover:text-apple"
            }`}
          >
            全部参数
          </button>
          {categories.map((cat) => {
            const active = activePicked?.includes(cat) ?? false;
            return (
              <button
                key={cat}
                type="button"
                onClick={() => toggleCategory(cat)}
                aria-pressed={active}
                className={`press rounded-full border px-3.5 py-1.5 text-xs duration-300 ${
                  active
                    ? "border-apple bg-apple/10 font-medium text-apple"
                    : "border-black/[0.08] bg-white/85 text-ink-soft hover:border-apple/35 hover:text-apple"
                }`}
              >
                {CATEGORY_LABELS[cat] ?? cat}
              </button>
            );
          })}
          <span className="ml-1 text-xs text-ash">
            {activePicked ? `已选 ${activePicked.length} / ${categories.length} 类` : `共 ${categories.length} 类`}
          </span>
        </div>
      </Reveal>

      {visibleRows.length === 0 ? (
        <Reveal className="glass rounded-3xl border border-black/[0.05] p-10 text-center text-sm text-ash">
          当前筛选下没有可显示的参数项，请调整上方「对比项目」或「显示相同参数」设置。
        </Reveal>
      ) : (
        <Reveal
          className="nums overflow-x-auto rounded-[22px] border border-black/[0.06] bg-white/80 shadow-[0_2px_20px_-8px_rgba(0,0,0,0.08)] backdrop-blur-xl"
          delay={80}
        >
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-black/[0.06] bg-canvas/80">
                <th className="sticky left-0 bg-[#f2f2f4]/95 px-4 py-3.5 text-left text-[13px] font-semibold text-ink-soft backdrop-blur">
                  参数
                </th>
                {data.variants.map((v) => (
                  <th key={v.variant_id} className="min-w-[200px] px-4 py-3.5 text-left align-top">
                    <Link
                      href={`/vehicles/${v.series_id}`}
                      className="text-[15px] font-semibold tracking-tight text-ink transition-colors duration-300 hover:text-apple"
                    >
                      {v.brand_name} {v.series_name}
                    </Link>
                    <p className="mt-0.5 text-xs font-normal text-ash">{v.display_name}</p>
                    <p className="mt-1 inline-block rounded-full bg-ice px-2 py-0.5 text-[11px] font-medium text-apple">
                      {ENERGY_LABELS[v.energy_type] ?? v.energy_type}
                    </p>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr className="border-b border-black/[0.06] bg-ice/60">
                <td className="sticky left-0 bg-[#e8f0fa]/95 px-4 py-3 text-[13px] font-semibold text-ink-soft backdrop-blur">
                  官方指导价
                </td>
                {data.variants.map((v) => (
                  <td key={v.variant_id} className="px-4 py-3 text-[15px] font-semibold tracking-tight text-apple">
                    {v.price_cny != null ? formatPrice(v.price_cny) : MISSING_LABEL}
                  </td>
                ))}
              </tr>
              {visibleRows.map((row) => {
                const key = `${row.category}|${row.fact_key}`;
                const common = commonKeys.has(key);
                return (
                  <tr
                    key={key}
                    className={`border-b border-black/[0.04] transition-colors duration-200 last:border-b-0 hover:bg-ice/40 ${
                      common ? "bg-canvas/50" : ""
                    }`}
                  >
                    <td className="sticky left-0 bg-white/95 px-4 py-2.5 text-[13px] text-ash backdrop-blur">
                      {CATEGORY_LABELS[row.category] ?? row.category} · {row.label}
                      {common && <span className="ml-1 text-[11px] text-hair">（相同）</span>}
                    </td>
                    {row.values.map((val, i) => (
                      <td key={i} className="px-4 py-2.5 text-[13px] font-medium text-ink-soft">
                        {val ?? MISSING_LABEL}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Reveal>
      )}
      <Reveal className="mt-4" delay={60}>
        <p className="text-xs leading-6 text-ash">
          对比对象为具体款型；不同工况（CLTC/NEDC/WLTC）的续航与油耗不直接比较。
        </p>
      </Reveal>
      {/* 分析未就绪时也渲染占位卡片：✨按钮的跳转目标在任何时刻都存在 */}
      {loading && !analysis && (
        <Reveal className="mt-6" delay={60}>
          <div
            id="analysis-panel"
            className="glass rounded-[22px] border border-apple/15 p-5 text-[13px] text-ash"
          >
            正在生成差异分析…
          </div>
        </Reveal>
      )}
      {analysis && (
        <AnalysisPanel
          analysis={analysis}
          aiComment={aiComment}
          names={new Map(data.variants.map((v) => [v.variant_id, `${v.brand_name} ${v.series_name}`]))}
          trims={new Map(data.variants.map((v) => [v.variant_id, v.display_name]))}
        />
      )}
    </div>
  );
}

function CompareContent() {
  const { data, error } = useComparison();

  return (
    <>
      {/* ── 页级 Hero ───────────────────────────────────────────────── */}
      <section className="relative overflow-hidden">
        <HeroGlow />
        <div className="relative mx-auto max-w-6xl px-4 pb-10 pt-12 text-center sm:px-6 sm:pt-14">
          <h1 className="text-[32px] font-semibold leading-tight tracking-tight text-ink sm:text-[42px]">
            <RiseText text="款型对比" startDelay={120} />
            <RiseText text="，" startDelay={430} />
            <RiseText text="差异一目了然" gradient startDelay={520} />
          </h1>
          <p
            className="animate-fade-up mx-auto mt-4 max-w-xl text-sm leading-6 text-ash sm:text-[15px]"
            style={{ animationDelay: "880ms" }}
          >
            从车型详情页选择具体款型加入对比，最多同时比较 {MAX_COMPARE} 个；对比链接可分享。
          </p>
          {data && data.variants.length > 0 && (
            <div className="animate-scale-in mt-6 flex justify-center" style={{ animationDelay: "200ms" }}>
              <button
                type="button"
                onClick={() => {
                  // 用户口径（2026-09-16）：点按钮 = 跳到页面里的确定性差异分析面板
                  //（先一句话结论 + 关键差异，细节可展开），不再自动弹开悬浮窗；
                  // 分析尚未生成（<2 款型 / 加载失败）时才回退为悬浮 Agent 介入。
                  const panel = document.getElementById("analysis-panel");
                  if (panel) {
                    panel.scrollIntoView({ behavior: "smooth", block: "start" });
                    window.dispatchEvent(new CustomEvent("dsh:flash-analysis"));
                    return;
                  }
                  const names = [...new Set(data.variants.map((v) => `${v.brand_name} ${v.series_name}`))];
                  const ids = data.variants.map((v) => v.variant_id).join("、");
                  const ask =
                    names.length === 1
                      ? `帮我分析一下 ${names[0]} 这款车怎么样、适合什么人。`
                      : `请从专业角度分析这几个款型的差异、帮我选一台适合我的：${names.join("、")}（款型ID：${ids}）。`;
                  askAgent(ask);
                }}
                className="press glass inline-flex items-center gap-1.5 rounded-full border border-apple/25 px-5 py-2.5 text-sm font-semibold text-apple shadow-md shadow-apple/10 hover:border-apple/45 hover:bg-ice/70"
              >
                ✨ 查看差异分析
              </button>
            </div>
          )}
        </div>
      </section>

      <main id="main-content" className="mx-auto max-w-6xl px-4 pb-8 sm:px-6">
        {error ? (
          <Reveal className="rounded-2xl border border-red-200/60 bg-red-50/90 p-4 text-sm text-red-600">
            {error}
          </Reveal>
        ) : data && data.variants.length > 0 ? (
          <CompareTable data={data} />
        ) : (
          <Reveal className="glass rounded-[28px] border border-black/[0.05] p-14 text-center">
            <span className="mx-auto flex h-20 w-20 items-center justify-center rounded-full bg-ice text-4xl shadow-inner shadow-apple/10">
              🚗
            </span>
            <p className="mt-5 text-[17px] font-semibold tracking-tight text-ink">尚未选择款型</p>
            <p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-ash">
              请前往任意车型详情页点击「加入对比」，即可开始比较具体款型的参数差异。
            </p>
            <div className="mt-6">
              <Link
                href="/"
                className="press inline-block rounded-full bg-apple px-6 py-2.5 text-sm font-medium text-white shadow-md shadow-apple/30 hover:bg-[#0077ed]"
              >
                去首页看销量榜 →
              </Link>
            </div>
          </Reveal>
        )}
      </main>
    </>
  );
}

export default function ComparePage() {
  return (
    <div>
      <SiteHeader />
      <Suspense
        fallback={
          <div className="mx-auto max-w-6xl space-y-4 px-4 py-12 sm:px-6">
            <div className="mx-auto h-10 w-56 animate-pulse rounded-full bg-white/70" />
            <div className="mx-auto h-4 w-80 animate-pulse rounded-full bg-white/50" />
            <div className="mt-8 h-64 animate-pulse rounded-[22px] bg-white/60" />
          </div>
        }
      >
        <CompareContent />
      </Suspense>
    </div>
  );
}
