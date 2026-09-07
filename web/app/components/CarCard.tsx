import Link from "next/link";
import { BODY_LABELS, ENERGY_LABELS, formatPriceRange, type HomeCard } from "@/lib/api";

const MISSING_SALES_LABEL = "暂无统一公开数据";

const ENERGY_CHIP: Record<string, string> = {
  BEV: "bg-ice text-apple ring-apple/12",
  PHEV: "bg-[#f2f1fe] text-[#5e5ce6] ring-[#5e5ce6]/12",
  EREV: "bg-[#fdf6ec] text-[#b25e09] ring-[#b25e09]/12",
  HEV: "bg-[#edf9f1] text-[#1d7d3f] ring-[#1d7d3f]/12",
  ICE: "bg-canvas text-ash ring-black/[0.06]",
};

const RANK_MEDAL: Record<number, string> = {
  1: "bg-gradient-to-br from-[#ffd60a] via-[#ffb800] to-[#ff9f0a] text-[#5c3d00] shadow-lg shadow-[#ffb800]/35",
  2: "bg-gradient-to-br from-[#e8e8ed] via-[#c7c7cc] to-[#a1a1a6] text-[#3a3a3c] shadow-lg shadow-[#a1a1a6]/30",
  3: "bg-gradient-to-br from-[#f5d9c0] via-[#e0a96d] to-[#c77f3e] text-[#4d2f10] shadow-lg shadow-[#c77f3e]/30",
};

/** 首页车型卡片。maxCount 为列表内销量最大值（可视化基准）。 */
export default function CarCard({
  card,
  index = 0,
  maxCount = 50000,
}: {
  card: HomeCard;
  index?: number;
  maxCount?: number;
}) {
  // 评审 P2：销量为 0 时不再显示 4% 的最小条
  const barWidth =
    card.sales_count > 0
      ? Math.max(4, Math.round((card.sales_count / Math.max(maxCount, 1)) * 100))
      : 0;

  return (
    <Link
      href={`/vehicles/${card.series_id}`}
      className="lift group flex h-full flex-col rounded-[26px] border border-black/[0.06] bg-white/85 p-5 shadow-[0_2px_16px_-6px_rgba(0,0,0,0.06)] backdrop-blur-xl hover:border-apple/25 hover:bg-white"
    >
      <div className="flex flex-1 items-start gap-3.5">
        {/* 排名徽章：前三名奖牌样式 + 第 1 名皇冠；其余为简洁数字章 */}
        {card.rank <= 3 ? (
          <span
            className={`relative mt-0.5 flex h-11 w-11 shrink-0 items-center justify-center rounded-full font-black transition-transform duration-500 ease-[cubic-bezier(0.34,1.56,0.64,1)] group-hover:scale-110 ${RANK_MEDAL[card.rank]}`}
          >
            {card.rank === 1 && <span className="absolute -top-2.5 text-sm drop-shadow">👑</span>}
            <span className="text-base">{card.rank}</span>
            <span className="absolute -bottom-1.5 rounded-full bg-white/95 px-1 text-[9px] font-bold leading-3 text-ash shadow-sm">
              名
            </span>
          </span>
        ) : (
          <span className="mt-0.5 flex h-11 w-11 shrink-0 flex-col items-center justify-center rounded-2xl bg-canvas ring-1 ring-black/[0.05] transition-colors duration-300 group-hover:bg-ice">
            <span className="text-[15px] font-semibold leading-4 text-ink-soft">
              {String(card.rank).padStart(2, "0")}
            </span>
            <span className="mt-0.5 text-[9px] font-medium leading-3 tracking-wide text-ash">RANK</span>
          </span>
        )}

        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="truncate text-[17px] font-semibold tracking-tight text-ink transition-colors duration-300 group-hover:text-apple">
              <span className="mr-1.5 text-sm font-normal text-ash">{card.brand_name}</span>
              {card.series_name}
            </h3>
            <span className="shrink-0 rounded-full bg-canvas px-2 py-0.5 text-[11px] text-ash">
              {BODY_LABELS[card.body_type ?? ""] ?? card.body_type ?? ""}
            </span>
          </div>

          <div className="mt-3.5 flex gap-4">
            {card.thumbnail_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={card.thumbnail_url}
                alt={card.series_name}
                className="h-20 w-28 shrink-0 rounded-2xl border border-black/[0.04] bg-canvas object-contain p-1 transition-transform duration-500 ease-[cubic-bezier(0.34,1.56,0.64,1)] group-hover:scale-[1.06]"
                loading="lazy"
              />
            ) : (
              <div className="brand-tile flex h-20 w-28 shrink-0 items-center justify-center rounded-2xl text-2xl font-semibold text-apple/30">
                {card.series_name.slice(0, 2)}
              </div>
            )}
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap gap-1">
                {(card.energy_types ?? []).map((t) => (
                  <span
                    key={t}
                    className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${ENERGY_CHIP[t] ?? "bg-canvas text-ash ring-black/[0.06]"}`}
                  >
                    {ENERGY_LABELS[t] ?? t}
                  </span>
                ))}
              </div>
              <p className="mt-2 flex items-baseline gap-1 text-[13px] text-ash">
                {card.month} 月销量
                {card.sales_count != null ? (
                  <span className="text-[17px] font-semibold tracking-tight text-ink">
                    {card.sales_count.toLocaleString()}
                    <span className="ml-0.5 text-xs font-normal text-ash">辆</span>
                  </span>
                ) : (
                  <span className="text-[13px] text-ash">{MISSING_SALES_LABEL}</span>
                )}
              </p>
              {card.sales_count != null && card.sales_count > 0 && (
                <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-black/[0.05]">
                  <div
                    className="bar-grow relative h-full rounded-full bg-gradient-to-r from-[#0a84ff] to-[#5e5ce6]"
                    style={{
                      width: `${barWidth}%`,
                      animationDelay: `${280 + Math.min(index, 12) * 70}ms`,
                    }}
                  >
                    <span className="bar-shine absolute inset-0 rounded-full" />
                  </div>
                </div>
              )}
              <p className="mt-2.5 text-sm font-medium text-ink-soft">
                {card.price_range.min === null && card.price_range.max === null && card.price_range_note
                  ? `官方指导价：${card.price_range_note}`
                  : formatPriceRange(card.price_range)}
              </p>
            </div>
          </div>
        </div>
      </div>

      <div className="hairline-t mt-auto pt-3 text-[11px] text-ash">
        <div className="flex items-center justify-between gap-3">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-[#34c759]" />
            <span className="truncate">数据来源：{card.source.name ?? "未标注"}</span>
          </span>
          <span className="flex shrink-0 items-center gap-1.5">
            更新：{card.data_updated_at ? card.data_updated_at.slice(0, 10) : "未标注"}
            <svg
              viewBox="0 0 20 20"
              fill="currentColor"
              className="h-3.5 w-3.5 -translate-x-1 text-apple opacity-0 transition-all duration-300 ease-[cubic-bezier(0.25,0.1,0.25,1)] group-hover:translate-x-0 group-hover:opacity-100"
            >
              <path
                fillRule="evenodd"
                d="M7.21 14.77a.75.75 0 0 1 .02-1.06L11.17 10 7.23 6.29a.75.75 0 1 1 1.04-1.08l4.5 4.25a.75.75 0 0 1 0 1.08l-4.5 4.25a.75.75 0 0 1-1.06-.02Z"
                clipRule="evenodd"
              />
            </svg>
          </span>
        </div>
      </div>
    </Link>
  );
}
