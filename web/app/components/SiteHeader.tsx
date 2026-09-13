"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

const NAV = [
  { href: "/", label: "首页", match: (p: string) => p === "/" },
  { href: "/vehicles", label: "全部车型", match: (p: string) => p === "/vehicles" },
  { href: "/compare", label: "SKU 对比", match: (p: string) => p.startsWith("/compare") },
  { href: "/favorites", label: "我的收藏", match: (p: string) => p.startsWith("/favorites") },
];

/** 液态玻璃导航（Apple 式）：常驻毛玻璃，滚动后浮现发丝线与柔和阴影。 */
export default function SiteHeader() {
  const pathname = usePathname();
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={`sticky top-0 z-30 bg-white/70 backdrop-blur-xl backdrop-saturate-150 transition-all duration-500 ease-[cubic-bezier(0.25,0.1,0.25,1)] ${
        scrolled
          ? "border-b border-black/[0.07] shadow-[0_10px_34px_-16px_rgba(0,0,0,0.14)]"
          : "border-b border-transparent"
      }`}
    >
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
        <Link href="/" className="group flex items-center gap-2.5">
          <span className="flex h-9 w-9 items-center justify-center rounded-[13px] bg-gradient-to-br from-[#0a84ff] via-apple to-[#5e5ce6] text-white shadow-md shadow-apple/30 transition-transform duration-500 ease-[cubic-bezier(0.34,1.56,0.64,1)] group-hover:scale-105 group-hover:rotate-3 group-active:scale-95">
            <svg viewBox="0 0 24 24" fill="currentColor" className="h-[18px] w-[18px]">
              <path d="M18.92 6.01C18.72 5.42 18.16 5 17.5 5h-11c-.66 0-1.21.42-1.42 1.01L3 12v7.5c0 .83.67 1.5 1.5 1.5S6 20.33 6 19.5V19h12v.5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5V12l-2.08-5.99ZM7.5 16.5A1.5 1.5 0 1 1 9 15a1.5 1.5 0 0 1-1.5 1.5Zm9 0a1.5 1.5 0 1 1 1.5-1.5 1.5 1.5 0 0 1-1.5 1.5ZM5.81 10l1.04-3h10.3l1.04 3H5.81Z" />
            </svg>
          </span>
          <span className="flex flex-col leading-tight">
            <span className="text-[17px] font-semibold tracking-tight text-ink">选车助手</span>
            <span className="hidden text-[11px] text-ash sm:block">家用新车 · 官方指导价 · 数据带来源</span>
          </span>
        </Link>

        <nav className="flex items-center gap-0.5 text-sm">
          {NAV.map((item) => {
            const active = item.match(pathname);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`press rounded-full px-3.5 py-1.5 duration-300 ${
                  active
                    ? "bg-apple/10 font-semibold text-apple"
                    : "text-ink-soft hover:bg-black/[0.05] hover:text-ink"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
