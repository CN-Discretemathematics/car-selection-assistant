"use client";

import { useEffect } from "react";

/** 深链定位：URL 带 ?variant={id} 时滚动到对应 SKU 卡片。 */
export default function ScrollToVariant({ variantId }: { variantId: number | null }) {
  useEffect(() => {
    if (variantId == null) return;
    const el = document.getElementById(`variant-${variantId}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [variantId]);
  return null;
}
