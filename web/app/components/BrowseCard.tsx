import Link from "next/link";
import { BODY_LABELS, ENERGY_CHIP, ENERGY_LABELS, formatPriceRange, SALES_TYPE_LABELS, type VehicleListItem } from "@/lib/api";

/** 全部车型浏览卡片（无排名；点进详情后可加对比）。 */
export default function BrowseCard({ item }: { item: VehicleListItem }) {
  return (
    <Link
      href={`/vehicles/${item.series_id}`}
      className="lift nums group flex h-full flex-col rounded-[22px] border border-black/[0.06] bg-white/85 p-5 shadow-[0_2px_16px_-6px_rgba(0,0,0,0.06)] backdrop-blur-xl hover:border-apple/25 hover:bg-white"
    >
      <div className="flex flex-1 gap-4">
        {item.thumbnail_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={item.thumbnail_url}
            alt={item.series_name}
            className="h-20 w-28 shrink-0 rounded-2xl border border-black/[0.04] bg-canvas object-contain p-1 transition-transform duration-500 ease-[cubic-bezier(0.34,1.56,0.64,1)] group-hover:scale-[1.06]"
            loading="lazy"
          />
        ) : (
          <div className="brand-tile flex h-20 w-28 shrink-0 items-center justify-center rounded-2xl text-2xl font-semibold text-apple/30">
            {item.series_name.slice(0, 2)}
          </div>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2">
            <h2 className="truncate text-[17px] font-semibold tracking-tight text-ink transition-colors duration-300 group-hover:text-apple">
              {/* 部分车系名自带品牌前缀（如「腾势Z9GT」），避免「腾势 腾势Z9GT」重复 */}
              {!item.series_name.startsWith(item.brand_name) && (
                <span className="mr-1.5 text-sm font-normal text-ash">{item.brand_name}</span>
              )}
              {item.series_name}
            </h2>
            <span className="shrink-0 rounded-full bg-canvas px-2 py-0.5 text-[11px] text-ash">
              {BODY_LABELS[item.body_type ?? ""] ?? item.body_type ?? ""}
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {(item.energy_types ?? []).map((t) => (
              <span
                key={t}
                className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${ENERGY_CHIP[t] ?? "bg-canvas text-ash ring-black/[0.06]"}`}
              >
                {ENERGY_LABELS[t] ?? t}
              </span>
            ))}
          </div>
          <p className="mt-2 text-sm font-medium text-ink-soft">
            {item.price_range.min === null && item.price_range.max === null && item.price_range_note
              ? `官方指导价：${item.price_range_note}`
              : formatPriceRange(item.price_range)}
          </p>
          {item.latest_sales.sales_count != null && (
            <p className="mt-0.5 text-xs text-ash">
              {item.latest_sales.month} 销量 {item.latest_sales.sales_count.toLocaleString()} 辆（
              {SALES_TYPE_LABELS[item.latest_sales.sales_type ?? ""] ?? "口径未标注"}）
            </p>
          )}
        </div>
      </div>
      <div className="hairline-t mt-auto pt-3 text-[11px] text-ash">
        <div className="flex items-center justify-between gap-3">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-[#34c759]" />
            <span className="truncate">数据来源：{item.source_name ?? "未标注"}</span>
          </span>
          <span className="flex shrink-0 items-center gap-1.5 whitespace-nowrap">
            更新：{item.data_updated_at ? item.data_updated_at.slice(0, 10) : "未标注"}
            <span className="hidden text-apple/80 transition-all duration-300 group-hover:translate-x-0.5 sm:inline">
              · 查看详情与款型 →
            </span>
          </span>
        </div>
      </div>
    </Link>
  );
}
