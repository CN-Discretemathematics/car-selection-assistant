"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { trackFilterClear } from "@/lib/agentTriggers";
import { BODY_OPTIONS, BRAND_OPTIONS, ENERGY_OPTIONS, HOME_SORT_OPTIONS } from "@/lib/filterOptions";
import FilterSelect from "./FilterSelect";
import SearchBar from "./SearchBar";

export default function HomeFilters() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // 表单以「万元」为单位展示，与后端（元）换算
  const wanFromYuan = (v: string | null) =>
    v && !Number.isNaN(Number(v)) ? String(Number(v) / 10000).replace(/\.?0+$/, "") : "";
  const [form, setForm] = useState({
    energy_type: searchParams.get("energy_type") ?? "",
    body_type: searchParams.get("body_type") ?? "",
    brand_type: searchParams.get("brand_type") ?? "",
    price_min: wanFromYuan(searchParams.get("price_min")),
    price_max: wanFromYuan(searchParams.get("price_max")),
    sort: searchParams.get("sort") ?? "desc",
  });

  function apply(next: typeof form) {
    setForm(next);
    const qs = new URLSearchParams();
    // 关键词来自筛选栏搜索框（非本表单字段）：改筛选时必须保留，否则搜索结果被静默丢弃
    const keyword = searchParams.get("q");
    if (keyword) qs.set("q", keyword);
    for (const [k, v] of Object.entries(next)) {
      if (!v) continue;
      if ((k === "price_min" || k === "price_max") && v !== "" && Number(v) >= 0 && !Number.isNaN(Number(v))) {
        qs.set(k, String(Math.round(Number(v) * 10000)));
      } else if (k !== "price_min" && k !== "price_max") {
        qs.set(k, v);
      }
    }
    router.push(qs.size ? `/?${qs.toString()}` : "/");
  }

  const inputCls =
    "h-9 rounded-full border border-black/[0.08] bg-white/85 px-3.5 text-[13px] text-ink-soft shadow-sm transition-all duration-300 ease-[cubic-bezier(0.25,0.1,0.25,1)] hover:border-apple/35 hover:bg-white focus:border-apple focus:bg-white focus:outline-none focus:ring-4 focus:ring-apple/12";

  return (
    <div
      // relative z-20：.glass 的入场动画会残留 transform，从而创建层叠上下文；
      // 不显式抬升的话，栏内的下拉浮层（z-50）会被下方卡片盖住
      className="glass animate-fade-up relative z-20 rounded-[22px] border border-black/[0.05] p-3"
      style={{ animationDelay: "1150ms" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1.5 pl-1.5 pr-2 text-xs font-semibold text-ash">
          <svg viewBox="0 0 24 24" fill="currentColor" className="h-3.5 w-3.5">
            <path d="M10 18h4v-2h-4v2zM3 6v2h18V6H3zm3 7h12v-2H6v2z" />
          </svg>
          筛选
        </span>
        <FilterSelect
          label="能源类型"
          value={form.energy_type}
          options={ENERGY_OPTIONS}
          onChange={(v) => apply({ ...form, energy_type: v })}
        />

        <FilterSelect
          label="车身类型"
          value={form.body_type}
          options={BODY_OPTIONS}
          onChange={(v) => apply({ ...form, body_type: v })}
        />

        <FilterSelect
          label="品牌类别"
          value={form.brand_type}
          options={BRAND_OPTIONS}
          onChange={(v) => apply({ ...form, brand_type: v })}
        />

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

        <FilterSelect
          label="排序"
          value={form.sort}
          options={HOME_SORT_OPTIONS}
          onChange={(v) => apply({ ...form, sort: v })}
        />

        {/* 搜索框固定放在排序右侧：回车即按关键词筛选本页榜单 */}
        <SearchBar placeholder="搜索车系/品牌" className="ml-auto sm:ml-0" />

        {searchParams.size > 0 && (
          <button
            type="button"
            onClick={() => {
              // §12.1：2 分钟内反复清空（≥3 次）说明筛选没筛明白 → 请 Agent 介入
              trackFilterClear(
                "home",
                "我在首页挑了很久都没挑明白，帮我梳理一下：预算、用途和人数都告诉我该怎么选车。",
              );
              setForm({ energy_type: "", body_type: "", brand_type: "", price_min: "", price_max: "", sort: "desc" });
              router.push("/");
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
