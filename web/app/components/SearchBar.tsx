"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { BODY_LABELS, formatPriceRange, type VehicleList, type VehicleListItem } from "@/lib/api";

const DEBOUNCE_MS = 220;
/** 下拉最多展示的结果数（超出走「查看全部」）。 */
const DROPDOWN_SIZE = 6;
const HINT = "试试：比亚迪 / Model Y / 腾势Z9GT";
/** 支持就地关键词筛选的页面（回车即筛当前列表，不跳页）；其它页面回退到 /vehicles?q=。 */
const INLINE_QUERY_PATHS = ["/", "/vehicles"];

interface SearchBarProps {
  placeholder?: string;
  className?: string;
}

/**
 * 筛选栏搜索（放在筛选栏「排序」右侧，首页与全部车型页共用）：
 * 输入即搜（防抖 220ms），下拉展示缩略图/品牌车系/价格区间，↑↓ 可键盘选择；
 * 回车＝把 q 写进当前页 URL 就地筛选，选择下拉项＝直达该车系详情，✕＝取消关键词筛选。
 * 数据来自 GET /api/v1/vehicles?q=（与 /home?q=、「问 Agent」共用同一套名称归一化匹配）。
 *
 * 内部用 useSearchParams 读取当前 q，Next 15 要求这类组件必须有 Suspense 边界，
 * 否则任何静态页（如 /ops/rag）预渲染会因 CSR bailout 失败——这里统一兜住，调用方无需关心。
 */
export default function SearchBar(props: SearchBarProps) {
  return (
    <Suspense fallback={<SearchBarSkeleton />}>
      <SearchBarInner {...props} />
    </Suspense>
  );
}

/** 加载/占位：尺寸与真实输入框一致，避免布局跳动。 */
function SearchBarSkeleton() {
  return <div className="h-9 w-[190px] animate-pulse rounded-full bg-white/70" aria-hidden />;
}

function SearchBarInner({
  placeholder = "搜索车系/品牌",
  className = "",
}: SearchBarProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [value, setValue] = useState("");
  const [items, setItems] = useState<VehicleListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // 相同关键词不重复请求（删除后又输入同一词时直接命中）
  const cacheRef = useRef<Map<string, VehicleList>>(new Map());

  // 输入框跟随 URL 的 q（前进/后退、清空筛选后保持一致）
  const urlKeyword = searchParams.get("q") ?? "";
  useEffect(() => {
    setValue(urlKeyword);
  }, [urlKeyword]);

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

  // 点击外部收起下拉
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  function go(href: string) {
    setOpen(false);
    setActive(-1);
    setValue("");
    router.push(href);
  }

  /** 把关键词写进当前页 URL（保留其它筛选条件），列表就地筛选。 */
  const inlineQuery = INLINE_QUERY_PATHS.includes(pathname);

  function applyInlineQuery(next: string | null) {
    const params = new URLSearchParams(searchParams.toString());
    if (next) params.set("q", next);
    else params.delete("q");
    params.delete("page"); // 换关键词回到第一页
    setOpen(false);
    setActive(-1);
    router.push(params.size ? `${pathname}?${params.toString()}` : pathname);
  }

  function submit() {
    if (active >= 0 && items[active]) {
      go(`/vehicles/${items[active].series_id}`);
      return;
    }
    if (inlineQuery) {
      // 已筛选同一关键词时不重复导航
      if (keyword !== urlKeyword) applyInlineQuery(keyword || null);
      else setOpen(false);
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
        className={`w-full rounded-full border border-black/[0.08] bg-white/85 py-2 pl-9 pr-9 text-[13.5px] text-ink shadow-sm transition-all duration-300 ease-[cubic-bezier(0.25,0.1,0.25,1)] placeholder:text-ash/70 hover:border-apple/35 hover:bg-white focus:border-apple focus:bg-white focus:outline-none focus:ring-4 focus:ring-apple/12 [&::-webkit-search-cancel-button]:hidden h-9 py-0 pl-8 pr-8 text-[13px]`}
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
            if (inlineQuery) {
              // 筛选栏：清空即取消关键词筛选，列表恢复
              applyInlineQuery(null);
              return;
            }
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
          className="animate-scale-in absolute right-0 top-[calc(100%+0.5rem)] z-50 w-[360px] max-w-[calc(100vw-2rem)] origin-top-right overflow-hidden rounded-[22px] border border-black/[0.08] bg-white p-1.5 shadow-[0_22px_50px_-14px_rgba(0,0,0,0.32)]"
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
                  className={`flex w-full items-center gap-3 rounded-[16px] px-2.5 py-2 text-left transition-colors duration-200 ${
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
                  onClick={() =>
                    inlineQuery
                      ? applyInlineQuery(keyword)
                      : go(`/vehicles?q=${encodeURIComponent(keyword)}`)
                  }
                  className="press mt-0.5 block w-full rounded-[16px] px-3 py-2 text-center text-xs font-medium text-apple hover:bg-ice"
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

  // 筛选栏内联：紧凑输入框（窄屏换行时占满一行）
  return (
    <div ref={boxRef} className={`relative w-[190px] max-w-full ${className}`}>
      {field}
    </div>
  );
}
