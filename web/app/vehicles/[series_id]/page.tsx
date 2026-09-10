import { notFound } from "next/navigation";
import Link from "next/link";
import AddToCompareButton from "@/app/components/AddToCompareButton";
import FavoriteButton from "@/app/components/FavoriteButton";
import HeroGlow from "@/app/components/HeroGlow";
import Reveal from "@/app/components/Reveal";
import RiseText from "@/app/components/RiseText";
import ScrollToVariant from "@/app/components/ScrollToVariant";
import SiteHeader from "@/app/components/SiteHeader";
import {
  BODY_LABELS,
  ENERGY_LABELS,
  SALES_TYPE_LABELS,
  fetchServerJson,
  formatPrice,
  formatPriceRange,
  type VariantOut,
  type VehicleDetail,
} from "@/lib/api";

const MISSING_LABEL = "官方资料未披露";

/** 车型详情页（PROJECT_PLAN.md §10）。 */
export default async function VehiclePage({
  params,
  searchParams,
}: {
  params: Promise<{ series_id: string }>;
  searchParams: Promise<{ variant?: string }>;
}) {
  const { series_id } = await params;
  const { variant: variantParam } = await searchParams;
  const id = Number(series_id);
  const highlightVariant = Number(variantParam);
  if (!Number.isInteger(id)) notFound();

  let detail: VehicleDetail | null = null;
  let variants: VariantOut[] = [];
  let detailError: string | null = null;
  let variantsError: string | null = null;
  try {
    detail = await fetchServerJson<VehicleDetail>(`/api/v1/vehicles/${id}`);
  } catch (err) {
    if (err instanceof Error && err.message.includes("404")) notFound();
    detailError = "数据暂时不可用，请稍后重试。";
  }
  if (detail) {
    try {
      variants = await fetchServerJson<VariantOut[]>(`/api/v1/vehicles/${id}/variants`);
    } catch {
      variantsError = "SKU 列表暂时不可用，请稍后重试。";
    }
  }

  if (!detail) {
    return (
      <div>
        <SiteHeader />
        <main className="mx-auto max-w-6xl px-4 py-16">
          <div className="glass rounded-3xl border border-black/[0.05] p-12 text-center">
            <p className="text-3xl">🛰️</p>
            <p className="mt-3 text-sm text-ash">{detailError ?? "车型不存在。"}</p>
          </div>
        </main>
      </div>
    );
  }

  const sales = detail.latest_sales;

  return (
    <div>
      <SiteHeader />

      {/* ── 产品页式 Hero：居中 + 极光氛围 + 车型图片 ───────────────── */}
      <section className="relative overflow-hidden">
        <HeroGlow />
        <div className="relative mx-auto max-w-6xl px-4 pb-12 pt-10 text-center sm:px-6">
          <Link
            href="/"
            className="glass press inline-flex items-center gap-1 rounded-full border border-black/[0.05] px-3.5 py-1.5 text-xs font-medium text-ash hover:text-apple"
          >
            ← 返回销量榜
          </Link>
          <p
            className="animate-fade-up mt-5 text-sm font-semibold tracking-wide text-apple"
            style={{ animationDelay: "120ms" }}
          >
            {detail.brand?.name}
          </p>
          <h1 className="mt-2 text-[34px] font-semibold leading-tight tracking-tight text-ink sm:text-[46px]">
            <RiseText text={detail.name} startDelay={220} />
          </h1>
          <div
            className="animate-fade-up mt-4 flex flex-wrap items-center justify-center gap-2 text-sm"
            style={{ animationDelay: "620ms" }}
          >
            {detail.positioning && <span className="text-ash">{detail.positioning}</span>}
            {detail.body_type && (
              <span className="rounded-full bg-canvas px-2.5 py-1 text-xs text-ink-soft ring-1 ring-black/[0.06]">
                {BODY_LABELS[detail.body_type] ?? detail.body_type}
              </span>
            )}
            {detail.energy_types.map((t) => (
              <span
                key={t}
                className="rounded-full bg-[#edf9f1] px-2.5 py-1 text-xs font-medium text-[#1d7d3f] ring-1 ring-[#1d7d3f]/12"
              >
                {ENERGY_LABELS[t] ?? t}
              </span>
            ))}
          </div>

          {detail.thumbnail_url && (
            <div
              className="animate-fade-up mx-auto mt-8 max-w-2xl"
              style={{ animationDelay: "760ms" }}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={detail.thumbnail_url}
                alt={`${detail.brand?.name ?? ""} ${detail.name}`}
                className="mx-auto max-h-64 w-auto max-w-full rounded-[28px] border border-black/[0.04] bg-white/70 object-contain p-6 shadow-[0_30px_60px_-30px_rgba(0,0,0,0.25)] backdrop-blur-xl"
              />
            </div>
          )}

          <div
            className="animate-fade-up mt-7 flex flex-wrap items-center justify-center gap-2.5"
            style={{ animationDelay: "880ms" }}
          >
            {detail.official_page_url && (
              <a
                href={detail.official_page_url}
                target="_blank"
                rel="noopener noreferrer"
                className="press inline-flex items-center gap-1.5 rounded-full bg-apple px-5 py-2.5 text-sm font-medium text-white shadow-md shadow-apple/30 hover:bg-[#0077ed]"
              >
                查看品牌官网车型页 ↗
              </a>
            )}
            <FavoriteButton vehicleId={detail.id} kind="series" />
          </div>
        </div>
      </section>

      <main className="mx-auto max-w-6xl px-4 sm:px-6">
        {/* ── 关键数据三联卡 ─────────────────────────────────────────── */}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Reveal className="h-full">
            <div className="lift glass h-full rounded-[24px] border border-black/[0.05] p-5">
              <p className="flex items-center gap-1.5 text-xs font-medium text-ash">
                <span className="h-1.5 w-1.5 rounded-full bg-apple" />
                官方指导价区间
              </p>
              <p className="mt-2.5 text-[22px] font-semibold tracking-tight text-ink">
                {detail.price_range.min === null && detail.price_range.max === null && detail.price_range_note
                  ? `官方指导价：${detail.price_range_note}`
                  : formatPriceRange(detail.price_range)}
              </p>
              <p className="mt-1.5 text-xs text-ash">只展示官方指导价，非成交价</p>
            </div>
          </Reveal>
          <Reveal className="h-full" delay={90}>
            <div className="lift glass h-full rounded-[24px] border border-black/[0.05] p-5">
              <p className="flex items-center gap-1.5 text-xs font-medium text-ash">
                <span className="h-1.5 w-1.5 rounded-full bg-[#64d2ff]" />
                月销量（{sales?.sales_type ? (SALES_TYPE_LABELS[sales.sales_type] ?? sales.sales_type) : "口径"}）
              </p>
              <p className="mt-2.5 text-[22px] font-semibold tracking-tight text-ink">
                {sales?.sales_count != null ? `${sales.sales_count.toLocaleString()} 辆` : "暂无统一公开数据"}
              </p>
              <p className="mt-1.5 text-xs text-ash">
                {sales?.month ? `${sales.month} · 来源：${sales.source_name ?? "未标注"}` : "缺少可靠车型级销量数据"}
              </p>
            </div>
          </Reveal>
          <Reveal className="h-full" delay={180}>
            <div className="lift glass h-full rounded-[24px] border border-black/[0.05] p-5">
              <p className="flex items-center gap-1.5 text-xs font-medium text-ash">
                <span className="h-1.5 w-1.5 rounded-full bg-[#5e5ce6]" />
                在售年款
              </p>
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {detail.model_years.length === 0 ? (
                  <span className="text-sm text-ash">{MISSING_LABEL}</span>
                ) : (
                  detail.model_years.map((y) => (
                    <span
                      key={y.id}
                      className="rounded-full bg-canvas px-2.5 py-1 text-[13px] text-ink-soft ring-1 ring-black/[0.05]"
                    >
                      {y.year_name}
                    </span>
                  ))
                )}
              </div>
              <p className="mt-1.5 text-xs text-ash">
                数据更新：{detail.data_updated_at ? detail.data_updated_at.slice(0, 10) : "未标注"}
              </p>
            </div>
          </Reveal>
        </div>

        {/* ── 在售 SKU ───────────────────────────────────────────────── */}
        <Reveal className="mt-14 flex items-baseline gap-2.5">
          <h2 className="text-[24px] font-semibold tracking-tight text-ink">在售 SKU</h2>
          <span className="rounded-full bg-apple/10 px-2.5 py-0.5 text-xs font-semibold text-apple">
            {variants.length} 款
          </span>
        </Reveal>
        {Number.isInteger(highlightVariant) && <ScrollToVariant variantId={highlightVariant} />}
        {variantsError ? (
          <Reveal className="mt-4 rounded-2xl border border-amber-300/40 bg-amber-50/90 p-4 text-sm text-amber-700">
            {variantsError}
          </Reveal>
        ) : variants.length === 0 ? (
          <Reveal className="mt-4 text-sm text-ash">暂无在售 SKU 数据（官方资料未披露）。</Reveal>
        ) : null}
        <div className="mt-4 grid grid-cols-1 gap-5 lg:grid-cols-2">
          {variants.map((v, index) => (
            <Reveal
              key={v.id}
              id={`variant-${v.id}`}
              delay={Math.min(index, 8) * 70}
              className="h-full"
            >
              <div
                className={`lift h-full rounded-[26px] border bg-white/85 p-6 shadow-[0_2px_16px_-6px_rgba(0,0,0,0.06)] backdrop-blur-xl ${
                  v.id === highlightVariant
                    ? "animate-ring border-apple/60 ring-2 ring-apple/15"
                    : "border-black/[0.06] hover:border-apple/25 hover:bg-white"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="text-[16px] font-semibold tracking-tight text-ink">{v.display_name}</p>
                    <p className="mt-1 text-xs leading-5 text-ash">
                      {v.year_name} · {v.powertrain} · {v.drivetrain}
                      {v.package ? ` · ${v.package}` : ""}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <FavoriteButton vehicleId={v.id} kind="variant" />
                    <AddToCompareButton variantId={v.id} />
                  </div>
                </div>
                <p className="text-gradient mt-4 text-[28px] font-semibold tracking-tight">
                  {v.official_price ? formatPrice(v.official_price.price_cny) : MISSING_LABEL}
                </p>
                <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-0 text-[13px]">
                  {v.spec_facts.slice(0, 8).map((f) => (
                    <div
                      key={`${f.category}-${f.fact_key}`}
                      className="flex justify-between gap-2 border-b border-black/[0.04] py-1.5"
                    >
                      <span className="shrink-0 text-ash">{f.label || f.fact_key}</span>
                      <span className="text-right font-medium text-ink-soft">{f.display}</span>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-[11px] text-ash">
                  缺失数据统一显示「{MISSING_LABEL}」，不做猜测补全。
                </p>
              </div>
            </Reveal>
          ))}
        </div>

        <Reveal className="mt-12 pb-4" delay={60}>
          <p className="text-xs leading-6 text-ash">
            本站不提供站内交易入口，也不跳转第三方经销商；价格与配置以品牌官网为准。
          </p>
        </Reveal>
      </main>
    </div>
  );
}
