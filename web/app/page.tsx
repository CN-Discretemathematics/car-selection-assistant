import { Suspense } from "react";
import CarCard from "./components/CarCard";
import HeroGlow from "./components/HeroGlow";
import HomeFilters from "./components/HomeFilters";
import Reveal from "./components/Reveal";
import RiseText from "./components/RiseText";
import SearchBar from "./components/SearchBar";
import SiteHeader from "./components/SiteHeader";
import { fetchServerJson, SALES_TYPE_LABELS, type HomeCard } from "@/lib/api";

interface SearchParams {
  energy_type?: string;
  body_type?: string;
  brand_type?: string;
  price_min?: string;
  price_max?: string;
  month?: string;
  sort?: string;
}

export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v) qs.set(k, v);
  }

  let cards: HomeCard[] = [];
  let error: string | null = null;
  try {
    cards = await fetchServerJson<HomeCard[]>(`/api/v1/home?${qs.toString()}`);
  } catch {
    error = "数据暂时不可用，请稍后重试。";
  }

  // 未指定月份时，后端回退到库内最新数据月（销量数据月中发布），标签随数据显示
  const monthLabel = params.month ?? cards[0]?.month ?? "最近完整自然月";
  // 目标月 = 最近完整自然月；数据月落后时提示「待发布，自动更新」
  const now = new Date();
  const expectedMonth =
    now.getMonth() === 0
      ? `${now.getFullYear() - 1}-12`
      : `${now.getFullYear()}-${String(now.getMonth()).padStart(2, "0")}`;
  const dataMonth = cards[0]?.month ?? "";
  const staleNote =
    !params.month && dataMonth && dataMonth !== expectedMonth
      ? `最新数据为 ${dataMonth}，${expectedMonth} 数据发布后自动更新`
      : null;
  const salesTypeLabel = cards.length
    ? SALES_TYPE_LABELS[cards[0].sales_type] ?? cards[0].sales_type
    : "";
  // 销量条可视化基准 = 列表内最大销量（不改变数据，仅做相对宽度）
  const maxSalesCount = cards.reduce((acc, c) => Math.max(acc, c.sales_count ?? 0), 0);

  return (
    <div>
      <SiteHeader />

      {/* ── Hero：极光氛围 + 逐字浮现标题（Apple 产品页式） ─────────── */}
      <section className="relative overflow-hidden">
        <HeroGlow />
        <div className="relative mx-auto max-w-6xl px-4 pb-12 pt-14 text-center sm:px-6 sm:pt-20">
          <p className="animate-fade-up text-sm font-semibold tracking-wide text-apple" style={{ animationDelay: "80ms" }}>
            选车助手 · 家用新车推荐
          </p>
          <h1 className="mx-auto mt-4 max-w-3xl text-[38px] font-semibold leading-[1.14] tracking-tight text-ink sm:text-[56px]">
            <RiseText text="找到适合你的" startDelay={160} />
            <br />
            <RiseText text="那一款车" gradient startDelay={560} />
          </h1>
          <p
            className="animate-fade-up mx-auto mt-5 max-w-xl text-[15px] leading-7 text-ash sm:text-base"
            style={{ animationDelay: "900ms" }}
          >
            真实销量数据 · 官方指导价 · 全部带来源与更新时间。
            <br className="hidden sm:block" />
            不知道从哪开始？直接搜车系，或点右下角「帮我选车」让 AI 梳理需求。
          </p>
          {/* 首页主入口：搜车系名/品牌名（与服务端同一套归一化匹配），回车进结果页 */}
          <div
            className="animate-fade-up mx-auto mt-7 max-w-xl"
            style={{ animationDelay: "1000ms" }}
          >
            <SearchBar variant="hero" placeholder="搜品牌或车系，如「比亚迪」「Z9GT」「Model Y」" />
          </div>
          <div
            className="animate-fade-up mt-5 flex flex-wrap items-center justify-center gap-2"
            style={{ animationDelay: "1050ms" }}
          >
            <span className="glass inline-flex items-center gap-1.5 rounded-full border border-black/[0.05] px-3.5 py-1.5 text-xs font-medium text-ink-soft">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#34c759]" />
              {monthLabel}
              {salesTypeLabel ? ` · ${salesTypeLabel}` : ""}
            </span>
            <span className="glass inline-flex items-center rounded-full border border-black/[0.05] px-3.5 py-1.5 text-xs font-medium text-ink-soft">
              只收录在范围内主流品牌
            </span>
            {staleNote && (
              <span className="inline-flex items-center rounded-full border border-amber-300/40 bg-amber-50/90 px-3.5 py-1.5 text-xs font-medium text-amber-700 backdrop-blur">
                {staleNote}
              </span>
            )}
          </div>
        </div>
      </section>

      <main className="mx-auto max-w-6xl px-4 sm:px-6">
        <Suspense
          fallback={
            <div className="h-16 animate-pulse rounded-3xl bg-white/60" aria-label="加载筛选器" />
          }
        >
          <HomeFilters />
        </Suspense>

        {/* ── 销量榜区块头 ───────────────────────────────────────────── */}
        <Reveal className="mb-5 mt-12 flex flex-wrap items-end justify-between gap-3">
          <h2 className="text-[26px] font-semibold tracking-tight text-ink">
            车型销量榜
            <span className="ml-2.5 text-sm font-normal text-ash">
              {monthLabel}
              {salesTypeLabel ? ` · ${salesTypeLabel}` : ""}
            </span>
          </h2>
          {!error && cards.length > 0 && (
            <span className="rounded-full border border-black/[0.06] bg-white/70 px-3 py-1 text-xs font-medium text-ash backdrop-blur">
              共 {cards.length} 个车系
            </span>
          )}
        </Reveal>

        {error ? (
          <Reveal className="glass mt-2 rounded-3xl border border-black/[0.05] p-12 text-center">
            <p className="text-3xl">🛰️</p>
            <p className="mt-3 text-sm text-ash">{error}</p>
          </Reveal>
        ) : cards.length === 0 ? (
          <Reveal className="glass mt-2 rounded-3xl border border-black/[0.05] p-12 text-center">
            <p className="text-3xl">🔍</p>
            <p className="mt-3 text-sm text-ash">暂无符合条件的数据，请调整筛选条件。</p>
          </Reveal>
        ) : (
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
            {cards.map((card, index) => (
              <Reveal key={card.series_id} delay={Math.min(index, 10) * 70} className="h-full">
                <CarCard card={card} index={index} maxCount={maxSalesCount} />
              </Reveal>
            ))}
          </div>
        )}

        <Reveal className="mt-12 space-y-2 pb-4 text-center" delay={80}>
          <p className="text-xs leading-6 text-ash">
            本站只展示新车官方指导价，交易、询价、优惠与库存信息请前往品牌官网车型页。
          </p>
          <p className="text-xs leading-6 text-ash">
            <a href="/privacy" className="underline-offset-4 transition hover:text-apple hover:underline">
              隐私政策
            </a>
            <span className="mx-1.5 text-hair">·</span>
            购车助手内容由 AI 生成，仅供参考。
          </p>
        </Reveal>
      </main>
    </div>
  );
}
