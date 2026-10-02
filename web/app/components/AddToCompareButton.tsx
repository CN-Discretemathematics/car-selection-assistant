"use client";

import { useCallback, useEffect, useState } from "react";
import { COMPARE_EVENT, readCompareIds, writeCompareIds } from "./CompareBar";

const MAX_COMPARE = 5;

/**
 * 「加入对比」按钮：把 SKU 加入本地对比栏（游客无状态，URL 分享）。
 *
 * **本组件只是 localStorage 的一个投影，不是第二份状态。** 此前用
 * `useState<boolean | null>(null)` 且**只在自己的 toggle 里**更新，造成两个可复现的错：
 *  1. 带已选状态进入页面时，按钮仍显示「加入对比」——与底部对比栏显示的「已选 N 个」
 *     当场矛盾；
 *  2. 从对比栏移除该款型后，按钮仍显示「已加入对比」+ 对勾。此时再点它，
 *     `readCompareIds()` 里已没有该 id，于是走「不在里面 → 重新加入」分支——
 *     **文案说移除、行为是加回**，用户点几次都删不掉。
 *
 * 修法对齐 `CompareBar.tsx` 里已有的正确实现：挂载时读一次 + 订阅
 * `compare-changed` 与 `storage`（跨标签页同步）。
 */
export default function AddToCompareButton({ variantId }: { variantId: number }) {
  const [selected, setSelected] = useState(false);

  useEffect(() => {
    const sync = () => setSelected(readCompareIds().includes(variantId));
    sync();
    window.addEventListener(COMPARE_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(COMPARE_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, [variantId]);

  const toggle = useCallback(() => {
    const ids = readCompareIds();
    if (ids.includes(variantId)) {
      writeCompareIds(ids.filter((id) => id !== variantId));
      setSelected(false);
    } else if (ids.length >= MAX_COMPARE) {
      window.alert(`最多同时对比 ${MAX_COMPARE} 个款型，请先移除部分车型。`);
    } else {
      writeCompareIds([...ids, variantId]);
      setSelected(true);
    }
  }, [variantId]);

  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={selected}
      className={`press inline-flex items-center gap-1 rounded-full border px-3.5 py-1.5 text-[13px] font-medium ${
        selected
          ? "border-transparent bg-gradient-to-r from-[#0a84ff] to-apple text-white shadow-md shadow-apple/30"
          : "border-black/[0.08] bg-white/85 text-ink-soft shadow-sm hover:border-apple/35 hover:text-apple"
      }`}
    >
      {selected ? (
        <svg viewBox="0 0 20 20" fill="currentColor" className="msg-pop h-3.5 w-3.5">
          <path
            fillRule="evenodd"
            d="M16.7 5.3a1 1 0 0 1 0 1.4l-8 8a1 1 0 0 1-1.4 0l-4-4a1 1 0 1 1 1.4-1.4L8 12.6l7.3-7.3a1 1 0 0 1 1.4 0Z"
            clipRule="evenodd"
          />
        </svg>
      ) : null}
      {selected ? "已加入对比" : "加入对比"}
    </button>
  );
}
