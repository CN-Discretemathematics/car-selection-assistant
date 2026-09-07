"use client";

import { useCallback, useState } from "react";
import { readCompareIds, writeCompareIds } from "./CompareBar";

const MAX_COMPARE = 5;

/** 「加入对比」按钮：把 SKU 加入本地对比栏（游客无状态，URL 分享）。 */
export default function AddToCompareButton({ variantId }: { variantId: number }) {
  const [selected, setSelected] = useState<boolean | null>(null);

  const toggle = useCallback(() => {
    const ids = readCompareIds();
    if (ids.includes(variantId)) {
      writeCompareIds(ids.filter((id) => id !== variantId));
      setSelected(false);
    } else if (ids.length >= MAX_COMPARE) {
      window.alert(`最多同时对比 ${MAX_COMPARE} 个 SKU，请先移除部分车型。`);
    } else {
      writeCompareIds([...ids, variantId]);
      setSelected(true);
    }
  }, [variantId]);

  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={selected ?? undefined}
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
