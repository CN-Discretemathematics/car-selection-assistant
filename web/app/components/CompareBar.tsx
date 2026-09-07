"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "compare_variant_ids";
const COMPARE_EVENT = "compare-changed";

export function readCompareIds(): number[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = JSON.parse(raw ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((n) => Number.isInteger(n)) : [];
  } catch {
    return [];
  }
}

export function writeCompareIds(ids: number[]) {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  window.dispatchEvent(new Event(COMPARE_EVENT));
}

export function compareHref(ids: number[]): string {
  return ids.length ? `/compare?variant_ids=${ids.join(",")}` : "/compare";
}

/** 底部对比栏：展示已选 SKU 数量（对比栏）。Liquid Glass 胶囊。 */
export default function CompareBar() {
  const [ids, setIds] = useState<number[]>([]);

  useEffect(() => {
    setIds(readCompareIds());
    const listener = () => setIds(readCompareIds());
    window.addEventListener(COMPARE_EVENT, listener);
    window.addEventListener("storage", listener);
    return () => {
      window.removeEventListener(COMPARE_EVENT, listener);
      window.removeEventListener("storage", listener);
    };
  }, []);

  const clear = useCallback(() => writeCompareIds([]), []);

  if (ids.length === 0) return null;

  return (
    <div className="glass-strong animate-slide-up fixed bottom-6 left-1/2 z-30 flex -translate-x-1/2 items-center gap-3 rounded-full border border-black/[0.07] px-5 py-2.5">
      <span className="animate-breathe flex h-6 min-w-6 items-center justify-center rounded-full bg-gradient-to-br from-[#0a84ff] to-[#5e5ce6] px-1.5 text-xs font-bold text-white shadow-sm shadow-apple/30">
        {ids.length}
      </span>
      <span className="whitespace-nowrap text-sm text-ash">
        已选 <span className="font-semibold text-ink">{ids.length}</span> 个 SKU
      </span>
      <Link
        href={compareHref(ids)}
        className="press whitespace-nowrap rounded-full bg-apple px-4 py-1.5 text-sm font-medium text-white shadow-md shadow-apple/30 hover:bg-[#0077ed]"
      >
        开始对比
      </Link>
      <button
        type="button"
        onClick={clear}
        className="press whitespace-nowrap rounded-full px-1.5 py-1 text-xs text-ash hover:text-ink"
      >
        清空
      </button>
    </div>
  );
}
