"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "compare_variant_ids";
// 导出而非各自复制字面量：对比栏的清空/移除、卡片上的「加入对比」按钮必须
// 订阅**同一个**事件名。此前 AddToCompareButton 根本没订阅（它只更新自己的
// useState），于是底部移除后按钮仍显示「已加入对比」，再点一下反而把款型加回来。
export const COMPARE_EVENT = "compare-changed";

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

/** 写入对比选择。返回**是否真的写进去了**——调用方必须能区分「已加入」与「没写成」。
 *
 * 为什么需要这个返回值：Safari 无痕模式 / 用户禁用 Cookie / 配额满时
 * `localStorage.setItem` 会**抛异常**。此前这里是裸调用，于是点「加入对比」
 * 抛未捕获错误、按钮纹丝不动，看起来像坏了；而同文件的 `readCompareIds`
 * 早就用 try/catch 挡住了读侧——**只防读不防写的不对称**是最容易漏掉的一种。
 * 写法对齐 `AgentChat.tsx:70-74`（同仓已有的正确处理）。
 *
 * 写失败时**不派发事件**：没改成却广播，等于告诉所有订阅者「已经变了」。
 */
export function writeCompareIds(ids: number[]): boolean {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    return false;
  }
  window.dispatchEvent(new Event(COMPARE_EVENT));
  return true;
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

  const clear = useCallback(() => {
    if (!writeCompareIds([])) {
      window.alert("浏览器禁用了本地存储，无法清空对比栏（可能处于无痕模式）。");
    }
  }, []);

  if (ids.length === 0) return null;

  return (
    <div className="compare-dock glass-strong animate-slide-up fixed bottom-6 left-1/2 z-30 flex -translate-x-1/2 items-center gap-3 rounded-full border border-black/[0.07] px-5 py-2.5">
      <span className="animate-breathe flex h-6 min-w-6 items-center justify-center rounded-full bg-gradient-to-br from-[#0a84ff] to-[#5e5ce6] px-1.5 text-xs font-bold text-white shadow-sm shadow-apple/30">
        {ids.length}
      </span>
      <span className="whitespace-nowrap text-sm text-ash">
        已选 <span className="font-semibold text-ink">{ids.length}</span> 个款型
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
