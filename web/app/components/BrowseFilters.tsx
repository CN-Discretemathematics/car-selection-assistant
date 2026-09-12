"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { trackFilterClear } from "@/lib/agentTriggers";

const BRAND_TYPES = [
  { value: "domestic_nev", label: "国产新能源" },
  { value: "luxury", label: "豪华" },
  { value: "japanese", label: "日系" },
  { value: "american", label: "美系" },
  { value: "german", label: "德系" },
  { value: "other_fuel", label: "其他燃油" },
];

/** 全部车型浏览页筛选（价格按万元展示，与后端（元）换算）。 */
export default function BrowseFilters() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const wanFromYuan = (v: string | null) =>
    v && !Number.isNaN(Number(v)) ? String(Number(v) / 10000).replace(/\.?0+$/, "") : "";
  const [form, setForm] = useState({
    energy_type: searchParams.get("energy_type") ?? "",
    body_type: searchParams.get("body_type") ?? "",
    brand_type: searchParams.get("brand_type") ?? "",
    price_min: wanFromYuan(searchParams.get("price_min")),
    price_max: wanFromYuan(searchParams.get("price_max")),
    sort: searchParams.get("sort") ?? "sales_desc",
  });

  function apply(next: typeof form, resetPage = true) {
    setForm(next);
    const qs = new URLSearchParams();
    // 关键词来自搜索框（非本表单字段）：改筛选时必须保留，否则搜索结果被静默丢弃
    const keyword = searchParams.get("q");
    if (keyword) qs.set("q", keyword);
    for (const [k, v] of Object.entries(next)) {
      if (!v) continue;
      if ((k === "price_min" || k === "price_max") && Number(v) >= 0 && !Number.isNaN(Number(v))) {
        qs.set(k, String(Math.round(Number(v) * 10000)));
      } else if (k !== "price_min" && k !== "price_max") {
        qs.set(k, v);
      }
    }
    const currentPage = searchParams.get("page");
    if (!resetPage && currentPage) qs.set("page", currentPage);
    router.push(qs.size ? `/vehicles?${qs.toString()}` : "/vehicles");
  }

  const inputCls =
    "h-9 rounded-full border border-black/[0.08] bg-white/85 px-3.5 text-[13px] text-ink-soft shadow-sm transition-all duration-300 ease-[cubic-bezier(0.25,0.1,0.25,1)] hover:border-apple/35 hover:bg-white focus:border-apple focus:bg-white focus:outline-none focus:ring-4 focus:ring-apple/12";

  return (
    <div
      className="glass animate-fade-up rounded-[22px] border border-black/[0.05] p-3"
      style={{ animationDelay: "980ms" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1.5 pl-1.5 pr-2 text-xs font-semibold text-ash">
          <svg viewBox="0 0 24 24" fill="currentColor" className="h-3.5 w-3.5">
            <path d="M10 18h4v-2h-4v2zM3 6v2h18V6H3zm3 7h12v-2H6v2z" />
          </svg>
          筛选
        </span>
        <select
          aria-label="能源类型"
          value={form.energy_type}
          onChange={(e) => apply({ ...form, energy_type: e.target.value })}
          className={inputCls}
        >
          <option value="">能源：全部</option>
          <option value="new_energy">新能源</option>
          <option value="fuel">燃油（含油混）</option>
          <option value="BEV">纯电</option>
          <option value="PHEV">插混</option>
          <option value="EREV">增程</option>
          <option value="HEV">油混</option>
          <option value="ICE">燃油</option>
        </select>
        <select
          aria-label="车身类型"
          value={form.body_type}
          onChange={(e) => apply({ ...form, body_type: e.target.value })}
          className={inputCls}
        >
          <option value="">车身：全部</option>
          <option value="sedan">轿车</option>
          <option value="suv">SUV</option>
          <option value="mpv">MPV</option>
          <option value="pickup">皮卡</option>
        </select>
        <select
          aria-label="品牌类别"
          value={form.brand_type}
          onChange={(e) => apply({ ...form, brand_type: e.target.value })}
          className={inputCls}
        >
          <option value="">品牌：全部</option>
          {BRAND_TYPES.map((b) => (
            <option key={b.value} value={b.value}>
              {b.label}
            </option>
          ))}
        </select>
        <div className="flex items-center gap-1">
          <input
            aria-label="最低价（万元）"
            type="number"
            min={0}
            placeholder="最低价(万)"
            value={form.price_min}
            onChange={(e) => apply({ ...form, price_min: e.target.value })}
            className={`${inputCls} w-28`}
          />
          <span className="text-hair">—</span>
          <input
            aria-label="最高价（万元）"
            type="number"
            min={0}
            placeholder="最高价(万)"
            value={form.price_max}
            onChange={(e) => apply({ ...form, price_max: e.target.value })}
            className={`${inputCls} w-28`}
          />
        </div>
        <select
          aria-label="排序"
          value={form.sort}
          onChange={(e) => apply({ ...form, sort: e.target.value })}
          className={inputCls}
        >
          <option value="sales_desc">销量从高到低</option>
          <option value="price_asc">价格从低到高</option>
          <option value="price_desc">价格从高到低</option>
        </select>
        {searchParams.size > 0 && (
          <button
            type="button"
            onClick={() => {
              // §12.1：2 分钟内反复清空（≥3 次）说明筛选没筛明白 → 请 Agent 介入
              trackFilterClear(
                "browse",
                "我在车型列表里挑了很久都没挑明白，帮我梳理一下：预算、用途和人数都告诉我该怎么选车。",
              );
              setForm({ energy_type: "", body_type: "", brand_type: "", price_min: "", price_max: "", sort: "sales_desc" });
              router.push("/vehicles");
            }}
            className="press h-9 rounded-full px-3.5 text-[13px] font-medium text-apple hover:bg-apple/8"
          >
            清空筛选
          </button>
        )}
      </div>
    </div>
  );
}
