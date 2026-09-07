import { Suspense } from "react";
import BrowseCard from "../components/BrowseCard";
import BrowseFilters from "../components/BrowseFilters";
import HeroGlow from "../components/HeroGlow";
import Pagination from "../components/Pagination";
import Reveal from "../components/Reveal";
import RiseText from "../components/RiseText";
import SiteHeader from "../components/SiteHeader";
import { fetchServerJson, type VehicleList } from "@/lib/api";

interface SearchParams {
  energy_type?: string;
  body_type?: string;
  brand_type?: string;
  price_min?: string;
  price_max?: string;
  sort?: string;
  page?: string;
}

/** 全部车型浏览页：任意车系 → 详情 → SKU 对比（不只限于销量榜 Top20）。 */
export default async function BrowsePage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v) qs.set(k, v);
  }
  if (!qs.has("sort")) qs.set("sort", "sales_desc");

  let data: VehicleList | null = null;
  let error: string | null = null;
  try {
    data = await fetchServerJson<VehicleList>(`/api/v1/vehicles?${qs.toString()}`);
  } catch {
    error = "数据暂时不可用，请稍后重试。";
  }

  const pageSize = data?.page_size ?? 20;
  const total = data?.total ?? 0;
  const totalPages = Math.max(Math.ceil(total / pageSize), 1);
  // 显示页码夹在 [1, totalPages]：直接输超大页码时高亮与「第 X / Y 页」保持合法
  const page = Math.min(Math.max(Number(params.page ?? "1") || 1, 1), totalPages);

  return (
    <div>
      <SiteHeader />

      {/* ── 页级 Hero ───────────────────────────────────────────────── */}
      <section className="relative overflow-hidden">
        <HeroGlow />
        <div className="relative mx-auto max-w-6xl px-4 pb-10 pt-12 text-center sm:px-6 sm:pt-14">
          <h1 className="text-[32px] font-semibold leading-tight tracking-tight text-ink sm:text-[42px]">
            <RiseText text="全部车型" startDelay={120} />
            <RiseText text="，" startDelay={420} />
            <RiseText text="一览无余" gradient startDelay={520} />
          </h1>
          <p
            className="animate-fade-up mx-auto mt-4 max-w-xl text-sm leading-6 text-ash sm:text-[15px]"
            style={{ animationDelay: "820ms" }}
          >
            浏览全部在范围车系（§2.1 品牌覆盖）；进入任意车型详情即可把具体 SKU 加入对比。
          </p>
          <div
            className="animate-fade-up mt-5 flex justify-center"
            style={{ animationDelay: "940ms" }}
          >
            <span className="glass inline-flex items-center gap-1.5 rounded-full border border-black/[0.05] px-3.5 py-1.5 text-xs font-medium text-ink-soft">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#34c759]" />
              {total} 个在售车系 · 全部带来源与更新时间
            </span>
          </div>
        </div>
      </section>

      <main className="mx-auto max-w-6xl px-4 sm:px-6">
        <Suspense
          fallback={
            <div className="h-16 animate-pulse rounded-3xl bg-white/60" aria-label="加载筛选器" />
          }
        >
          <BrowseFilters />
        </Suspense>

        {error ? (
          <Reveal className="glass mt-8 rounded-3xl border border-black/[0.05] p-12 text-center">
            <p className="text-3xl">🛰️</p>
            <p className="mt-3 text-sm text-ash">{error}</p>
          </Reveal>
        ) : (data?.items.length ?? 0) === 0 ? (
          <Reveal className="glass mt-8 rounded-3xl border border-black/[0.05] p-12 text-center">
            <p className="text-3xl">🔍</p>
            <p className="mt-3 text-sm text-ash">暂无符合条件的数据，请调整筛选条件。</p>
          </Reveal>
        ) : (
          <div className="mt-6 grid grid-cols-1 gap-5 md:grid-cols-2">
            {(data?.items ?? []).map((item, index) => (
              <Reveal key={item.series_id} delay={Math.min(index, 10) * 70} className="h-full">
                <BrowseCard item={item} />
              </Reveal>
            ))}
          </div>
        )}

        {/* 分页：页码直跳 + 跳页输入框（不只上一页/下一页） */}
        <Reveal>
          <Pagination
            page={page}
            totalPages={totalPages}
            basePath="/vehicles"
            query={qs}
            total={total}
          />
        </Reveal>

        <Reveal className="mt-10 pb-4 text-center" delay={60}>
          <p className="text-xs leading-6 text-ash">
            本站只展示新车官方指导价，交易、询价、优惠与库存信息请前往品牌官网车型页。
          </p>
        </Reveal>
      </main>
    </div>
  );
}
