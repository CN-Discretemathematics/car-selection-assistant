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
          className="overflow-x-auto rounded-[26px] border border-black/[0.06] bg-white/80 shadow-[0_2px_20px_-8px_rgba(0,0,0,0.08)] backdrop-blur-xl"
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
                    {v.official_page_url && (
                      <a
                        href={v.official_page_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="mt-1 block text-xs font-normal text-apple underline-offset-4 hover:underline"
                      >
                        官方车型页 ↗
                      </a>
                    )}
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
          对比对象为具体 SKU；缺失数据统一显示「{MISSING_LABEL}」，不同工况（CLTC/NEDC/WLTC）的续航/油耗不直接比较。
        </p>
      </Reveal>
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
            <RiseText text="SKU 对比" startDelay={120} />
            <RiseText text="，" startDelay={430} />
            <RiseText text="差异一目了然" gradient startDelay={520} />
          </h1>
          <p
            className="animate-fade-up mx-auto mt-4 max-w-xl text-sm leading-6 text-ash sm:text-[15px]"
            style={{ animationDelay: "880ms" }}
          >
            从车型详情页选择 SKU 加入对比，最多同时比较 {MAX_COMPARE} 个；对比链接可分享。
          </p>
          {data && data.variants.length > 0 && (
            <div className="animate-scale-in mt-6 flex justify-center" style={{ animationDelay: "200ms" }}>
              <button
                type="button"
                onClick={() => {
                  // §12.1：对比页「帮我分析差异」→ 悬浮 Agent 介入，带上当前对比车系
                  const names = [...new Set(data.variants.map((v) => `${v.brand_name} ${v.series_name}`))];
                  const text =
                    names.length === 1
                      ? `帮我分析一下 ${names[0]} 这款车怎么样、适合什么人。`
                      : `请帮我分析这些车型的差异，帮我选一台适合我的：${names.join("、")}。`;
                  askAgent(text);
                }}
                className="press glass inline-flex items-center gap-1.5 rounded-full border border-apple/25 px-5 py-2.5 text-sm font-semibold text-apple shadow-md shadow-apple/10 hover:border-apple/45 hover:bg-ice/70"
              >
                ✨ 帮我分析差异
              </button>
            </div>
          )}
        </div>
      </section>

      <main className="mx-auto max-w-6xl px-4 pb-8 sm:px-6">
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
            <p className="mt-5 text-[17px] font-semibold tracking-tight text-ink">尚未选择 SKU</p>
            <p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-ash">
              请前往任意车型详情页点击「加入对比」，即可开始比较具体 SKU 的参数差异。
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
            <div className="mt-8 h-64 animate-pulse rounded-[26px] bg-white/60" />
          </div>
        }
      >
        <CompareContent />
      </Suspense>
    </div>
  );
}
