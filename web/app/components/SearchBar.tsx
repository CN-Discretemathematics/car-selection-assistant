"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { BODY_LABELS, formatPriceRange, type VehicleList, type VehicleListItem } from "@/lib/api";

const DEBOUNCE_MS = 220;
/** 下拉最多展示的结果数（超出走「查看全部」）。 */
const DROPDOWN_SIZE = 6;
const HINT = "试试：比亚迪 / Model Y / 腾势Z9GT";

interface SearchBarProps {
  /**
   * header：导航内联搜索（≥lg 常显输入框；sm~lg 折叠为图标 + 浮层，避免挤压导航）；
   * hero：整行大搜索框（首页 Hero、移动端车型页）。
   */
  variant?: "header" | "hero";
  placeholder?: string;
  className?: string;
}

/**
 * 车系搜索：输入即搜（防抖 220ms），下拉展示缩略图/品牌车系/价格区间，
 * 回车直达全部结果页 /vehicles?q=…，↑↓ 可键盘选择。
 * 数据来自 GET /api/v1/vehicles?q=（与「问 Agent」共用同一套名称归一化匹配）。
 */
export default function SearchBar({
  variant = "header",
  placeholder = "搜品牌或车系，如「比亚迪」「Z9GT」",
  className = "",
}: SearchBarProps) {
  const router = useRouter();
  const [value, setValue] = useState("");
  const [items, setItems] = useState<VehicleListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false); // header 变体在窄屏的浮层
  const [active, setActive] = useState(-1);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // 相同关键词不重复请求（删除后又输入同一词时直接命中）
  const cacheRef = useRef<Map<string, VehicleList>>(new Map());

  const keyword = value.trim();

  useEffect(() => {
    if (!keyword) {
      setItems([]);
      setTotal(0);
      setFailed(false);
      setLoading(false);
      return;
    }
    const cached = cacheRef.current.get(keyword);
    if (cached) {
      setItems(cached.items);
      setTotal(cached.total);
      setFailed(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    const timer = window.setTimeout(async () => {
      try {
        const res = await fetch(
          `/api/v1/vehicles?q=${encodeURIComponent(keyword)}&page_size=${DROPDOWN_SIZE}`,
          { signal: controller.signal, cache: "no-store" },
        );
        if (!res.ok) throw new Error(String(res.status));
        const data = (await res.json()) as VehicleList;
        // 上限 50 条：单次会话内常搜词有限，避免无限增长
        if (cacheRef.current.size >= 50) cacheRef.current.clear();
        cacheRef.current.set(keyword, data);
        setItems(data.items);
        setTotal(data.total);
        setFailed(false);
      } catch (err) {
        if ((err as Error)?.name !== "AbortError") {
          setItems([]);
          setTotal(0);
          setFailed(true);
        }
      } finally {
        setLoading(false);
      }
    }, DEBOUNCE_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [keyword]);

  // 点击外部 / Esc 收起下拉与窄屏浮层
  useEffect(() => {
    if (!open && !expanded) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
        setExpanded(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open, expanded]);

  useEffect(() => {
    if (expanded) inputRef.current?.focus();
  }, [expanded]);

  function go(href: string) {
    setOpen(false);
    setExpanded(false);
    setActive(-1);
    setValue("");
    router.push(href);
  }

  function submit() {
    if (active >= 0 && items[active]) {
      go(`/vehicles/${items[active].series_id}`);
      return;
    }
    if (keyword) go(`/vehicles?q=${encodeURIComponent(keyword)}`);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown" && items.length > 0) {
      e.preventDefault();
      setOpen(true);
      setActive((i) => (i + 1) % items.length);
    } else if (e.key === "ArrowUp" && items.length > 0) {
      e.preventDefault();
      setOpen(true);
      setActive((i) => (i <= 0 ? items.length - 1 : i - 1));
    } else if (e.key === "Enter") {
      // 中文输入法组合中（拼音候选）的回车不触发跳转
      if (e.nativeEvent.isComposing) return;
      e.preventDefault();
      submit();
    } else if (e.key === "Escape") {
      if (open) setOpen(false);
      else setExpanded(false);
    }
  }

  const showPanel = open && keyword.length > 0;

  const field = (
    <div className="relative">
      <span className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-ash">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4">
          <circle cx="8.5" cy="8.5" r="5.5" />
          <path d="M12.8 12.8 17 17" strokeLinecap="round" />
        </svg>
      </span>
      <input
        ref={inputRef}
        type="search"
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          setActive(-1);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        aria-label="搜索品牌或车系"
        role="combobox"
        aria-expanded={showPanel}
        aria-controls="series-search-results"
        className={`w-full rounded-full border border-black/[0.08] bg-white/85 py-2 pl-9 pr-9 text-[13.5px] text-ink shadow-sm transition-all duration-300 ease-[cubic-bezier(0.25,0.1,0.25,1)] placeholder:text-ash/70 hover:border-apple/35 hover:bg-white focus:border-apple focus:bg-white focus:outline-none focus:ring-4 focus:ring-apple/12 [&::-webkit-search-cancel-button]:hidden ${
          variant === "hero" ? "py-2.5 pl-10 text-sm" : ""
        }`}
      />
      {loading && (
        <span
          aria-hidden
          className="absolute right-3.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin rounded-full border-2 border-apple/25 border-t-apple"
        />
      )}
      {!loading && value && (
        <button
          type="button"
          onClick={() => {
            setValue("");
            setActive(-1);
            inputRef.current?.focus();
          }}
          aria-label="清空搜索"
          className="press absolute right-3 top-1/2 flex h-5 w-5 -translate-y-1/2 items-center justify-center rounded-full bg-black/[0.07] text-[11px] text-ash hover:bg-black/15 hover:text-ink"
        >
          ✕
        </button>
      )}

      {showPanel && (
        <div
          id="series-search-results"
          role="listbox"
          className={`animate-scale-in absolute top-[calc(100%+0.5rem)] z-50 overflow-hidden rounded-[22px] border border-black/[0.07] bg-white/95 p-1.5 shadow-[0_22px_50px_-18px_rgba(0,0,0,0.28)] backdrop-blur-xl ${
            variant === "hero"
              ? "left-0 right-0 origin-top"
              : // 导航栏输入框只有 210px：下拉比输入框宽，右对齐向左展开，条目才读得全
                "right-0 w-[360px] max-w-[calc(100vw-2rem)] origin-top-right"
          }`}
        >
          {failed ? (
            <p className="px-3 py-3 text-xs leading-5 text-ash">搜索服务暂时不可用，请稍后重试。</p>
          ) : items.length === 0 ? (
            <p className="px-3 py-3 text-xs leading-5 text-ash">
              {loading ? (
                "搜索中…"
              ) : (
                <>
                  没找到「{keyword}」相关车系。
                  <br />
                  {HINT}
                </>
              )}
            </p>
          ) : (
            <>
              {items.map((item, i) => (
                <button
                  key={item.series_id}
                  type="button"
                  role="option"
                  aria-selected={i === active}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => go(`/vehicles/${item.series_id}`)}
                  className={`flex w-full items-center gap-3 rounded-[17px] px-2.5 py-2 text-left transition-colors duration-200 ${
                    i === active ? "bg-ice" : "hover:bg-canvas"
                  }`}
                >
                  {item.thumbnail_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={item.thumbnail_url}
                      alt=""
                      loading="lazy"
                      className="h-10 w-14 shrink-0 rounded-xl border border-black/[0.04] bg-canvas object-contain p-0.5"
                    />
                  ) : (
                    <span className="brand-tile flex h-10 w-14 shrink-0 items-center justify-center rounded-xl text-sm font-semibold text-apple/30">
                      {item.series_name.slice(0, 2)}
                    </span>
                  )}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13.5px] font-medium text-ink">
                      {/* 部分车系名自带品牌前缀（如「腾势Z9GT」），避免「腾势 腾势Z9GT」重复 */}
                      {!item.series_name.startsWith(item.brand_name) && (
                        <span className="mr-1.5 font-normal text-ash">{item.brand_name}</span>
                      )}
                      {item.series_name}
                    </span>
                    <span className="block truncate text-[11px] text-ash">
                      {formatPriceRange(item.price_range)}
                      {item.body_type ? ` · ${BODY_LABELS[item.body_type] ?? item.body_type}` : ""}
                    </span>
                  </span>
                  {i === active && <span className="shrink-0 text-[11px] text-apple">回车进入</span>}
                </button>
              ))}
              {total > items.length && (
                <button
                  type="button"
                  onClick={() => go(`/vehicles?q=${encodeURIComponent(keyword)}`)}
                  className="press mt-0.5 block w-full rounded-[17px] px-3 py-2 text-center text-xs font-medium text-apple hover:bg-ice"
                >
                  查看全部 {total} 个结果 →
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );

  if (variant === "hero") {
    return <div className={`relative w-full ${className}`}>{field}</div>;
  }

  return (
    <div ref={boxRef} className={`relative ${className}`}>
      {/* 窄屏（<lg）：只放图标，点开浮层输入，避免挤压导航 */}
      <button
        type="button"
        onClick={() => {
          setExpanded(true);
          setOpen(true);
        }}
        aria-label="搜索品牌或车系"
        className="press hidden h-9 w-9 items-center justify-center rounded-full text-ink-soft hover:bg-black/[0.05] hover:text-ink sm:flex lg:hidden"
      >
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-[18px] w-[18px]">
          <circle cx="8.5" cy="8.5" r="5.5" />
          <path d="M12.8 12.8 17 17" strokeLinecap="round" />
        </svg>
      </button>

      <div
        className={`${
          expanded ? "fixed inset-x-3 top-[3.75rem] z-50" : "hidden"
        } lg:static lg:block lg:w-[210px] lg:transition-[width] lg:duration-500 lg:ease-[cubic-bezier(0.25,0.1,0.25,1)] lg:focus-within:w-[260px]`}
      >
        {field}
      </div>
    </div>
  );
}
