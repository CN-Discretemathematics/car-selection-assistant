interface PaginationProps {
  /** 当前页（1-based）。 */
  page: number;
  /** 总页数（≥1）。 */
  totalPages: number;
  /** 基础路径，如 /vehicles。 */
  basePath: string;
  /** 除 page 外的查询参数（组件内部会去掉 page）。 */
  query: URLSearchParams;
  /** 总条数（可选，用于「共 N 款」文案）。 */
  total?: number;
}

/** 页码序列：总数少时全列；多时「1 … 当前±1 … 末页」折叠。 */
function pageList(current: number, total: number): (number | "…")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages: (number | "…")[] = [1];
  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);
  if (start > 2) pages.push("…");
  for (let p = start; p <= end; p += 1) pages.push(p);
  if (end < total - 1) pages.push("…");
  pages.push(total);
  return pages;
}

/** 生成带查询参数的分页链接（page=1 时省略 page 参数）。 */
function pageHref(basePath: string, query: URLSearchParams, p: number): string {
  const q = new URLSearchParams();
  for (const [k, v] of query.entries()) {
    if (k !== "page") q.set(k, v);
  }
  if (p > 1) q.set("page", String(p));
  const qs = q.toString();
  return qs ? `${basePath}?${qs}` : basePath;
}

const navButton = "press flex h-10 items-center rounded-full border px-4 text-sm shadow-sm";
const enabled =
  "border-black/[0.08] bg-white/85 text-ink-soft hover:border-apple/35 hover:bg-white hover:text-apple";
const disabled = "cursor-not-allowed border-black/[0.04] bg-white/40 text-hair shadow-none";
const pageButton = "press flex h-10 min-w-10 items-center justify-center rounded-full border px-3 text-sm shadow-sm";
const pageInactive =
  "border-black/[0.08] bg-white/85 text-ink-soft hover:border-apple/35 hover:bg-white hover:text-apple";
const pageActive = "border-apple bg-apple font-semibold text-white shadow-md shadow-apple/25";

export default function Pagination({
  page,
  totalPages,
  basePath,
  query,
  total,
}: PaginationProps) {
  if (totalPages <= 1) {
    return (
      <div className="mt-10 text-center text-sm text-ash">
        共 {total ?? 0} 款
      </div>
    );
  }

  return (
    <div className="mt-10 flex flex-col items-center gap-5">
      <nav aria-label="分页" className="flex flex-wrap items-center justify-center gap-1.5 text-sm">
        <a
          href={page > 1 ? pageHref(basePath, query, page - 1) : undefined}
          aria-disabled={page <= 1}
          className={`${navButton} ${page <= 1 ? disabled : enabled}`}
        >
          ← 上一页
        </a>

        {pageList(page, totalPages).map((p, i) =>
          p === "…" ? (
            <span key={`gap-${i}`} className="px-1.5 text-hair">
              …
            </span>
          ) : (
            <a
              key={p}
              href={pageHref(basePath, query, p)}
              aria-current={p === page ? "page" : undefined}
              className={`${pageButton} ${p === page ? pageActive : pageInactive}`}
            >
              {p}
            </a>
          ),
        )}

        <a
          href={page < totalPages ? pageHref(basePath, query, page + 1) : undefined}
          aria-disabled={page >= totalPages}
          className={`${navButton} ${page >= totalPages ? disabled : enabled}`}
        >
          下一页 →
        </a>
      </nav>

      <form
        method="get"
        action={basePath}
        className="glass flex flex-wrap items-center justify-center gap-2 rounded-full border border-black/[0.05] px-4 py-2 text-sm text-ash"
      >
        {[...query.entries()]
          .filter(([k]) => k !== "page")
          .map(([k, v]) => (
            <input key={k} type="hidden" name={k} value={v} />
          ))}
        <span>
          第 <span className="font-semibold text-ink">{page}</span> / {totalPages} 页
          {total !== undefined ? ` · 共 ${total} 款` : ""}
        </span>
        <span className="text-hair">|</span>
        <label htmlFor="page-jump" className="whitespace-nowrap">
          跳至第
        </label>
        <input
          id="page-jump"
          type="number"
          name="page"
          min={1}
          max={totalPages}
          defaultValue={page}
          className="h-8 w-16 rounded-full border border-black/[0.08] bg-white/90 px-2 text-center text-ink transition focus:border-apple focus:outline-none focus:ring-4 focus:ring-apple/12"
        />
        <span>页</span>
        <button
          type="submit"
          className="press h-8 rounded-full bg-apple px-3.5 text-[13px] font-medium text-white shadow-sm shadow-apple/25 hover:bg-[#0077ed]"
        >
          跳转
        </button>
      </form>
    </div>
  );
}
