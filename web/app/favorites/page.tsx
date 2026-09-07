"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import HeroGlow from "@/app/components/HeroGlow";
import Reveal from "@/app/components/Reveal";
import RiseText from "@/app/components/RiseText";
import SiteHeader from "@/app/components/SiteHeader";
import { deleteAccount, getToken, listFavorites, setToken } from "@/lib/auth";
import { formatPrice, type FavoriteOut } from "@/lib/api";

export default function FavoritesPage() {
  const [token] = useState<string | null>(() => getToken());
  const [items, setItems] = useState<FavoriteOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    listFavorites(token)
      .then(setItems)
      .catch((err) => setError(err instanceof Error ? err.message : "加载失败"));
  }, [token]);

  async function onDeleteAccount() {
    if (!token) return;
    if (!window.confirm("确定注销账号？注销后无法登录，收藏数据将不再可访问。")) return;
    try {
      await deleteAccount(token);
      setToken(null);
      window.location.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "注销失败");
    }
  }

  return (
    <div>
      <SiteHeader />

      {/* ── 页级 Hero ───────────────────────────────────────────────── */}
      <section className="relative overflow-hidden">
        <HeroGlow />
        <div className="relative mx-auto max-w-6xl px-4 pb-10 pt-12 text-center sm:px-6 sm:pt-14">
          <h1 className="text-[32px] font-semibold leading-tight tracking-tight text-ink sm:text-[42px]">
            <RiseText text="我的收藏" startDelay={120} />
          </h1>
          <p
            className="animate-fade-up mx-auto mt-4 max-w-xl text-sm leading-6 text-ash sm:text-[15px]"
            style={{ animationDelay: "560ms" }}
          >
            收藏的车型系列与 SKU（仅存账号邮箱，不收集其他信息）。
          </p>
        </div>
      </section>

      <main className="mx-auto max-w-6xl px-4 pb-8 sm:px-6">
        {!token ? (
          <Reveal className="glass rounded-[28px] border border-black/[0.05] p-14 text-center">
            <span className="mx-auto flex h-20 w-20 items-center justify-center rounded-full bg-ice text-4xl shadow-inner shadow-apple/10">
              🔐
            </span>
            <p className="mt-5 text-[17px] font-semibold tracking-tight text-ink">登录后才能查看收藏</p>
            <p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-ash">
              在任意车型详情页点击「收藏」即可用邮箱验证码登录。
            </p>
          </Reveal>
        ) : error ? (
          <Reveal className="rounded-2xl border border-red-200/60 bg-red-50/90 p-4 text-center text-sm text-red-600">
            {error}
          </Reveal>
        ) : items === null ? (
          <div className="mt-8 grid grid-cols-1 gap-4 md:grid-cols-2" aria-label="加载中">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="h-20 animate-pulse rounded-[24px] bg-white/60" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <Reveal className="glass rounded-[28px] border border-black/[0.05] p-14 text-center">
            <span className="mx-auto flex h-20 w-20 items-center justify-center rounded-full bg-ice text-4xl shadow-inner shadow-apple/10">
              🚗
            </span>
            <p className="mt-5 text-[17px] font-semibold tracking-tight text-ink">暂无收藏</p>
            <p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-ash">去首页看看销量榜吧。</p>
            <div className="mt-6">
              <Link
                href="/"
                className="press inline-block rounded-full bg-apple px-6 py-2.5 text-sm font-medium text-white shadow-md shadow-apple/30 hover:bg-[#0077ed]"
              >
                去首页看销量榜 →
              </Link>
            </div>
          </Reveal>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {items.map((item, index) => {
              const href =
                item.kind === "series" || item.series_id == null
                  ? `/vehicles/${item.vehicle_id}`
                  : `/vehicles/${item.series_id}?variant=${item.vehicle_id}`;
              return (
                <Reveal key={`${item.kind}-${item.vehicle_id}`} delay={Math.min(index, 10) * 60} className="h-full">
                  <Link
                    href={href}
                    className="lift group flex h-full items-center justify-between rounded-[24px] border border-black/[0.06] bg-white/85 p-4.5 shadow-[0_2px_16px_-6px_rgba(0,0,0,0.06)] backdrop-blur-xl hover:border-apple/25 hover:bg-white"
                  >
                    <div className="flex min-w-0 items-center gap-3.5">
                      <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-ice to-[#f2f1fe] text-lg shadow-inner shadow-apple/5 transition-transform duration-500 ease-[cubic-bezier(0.34,1.56,0.64,1)] group-hover:scale-110">
                        {item.kind === "series" ? "🚙" : "⚙️"}
                      </span>
                      <div className="min-w-0">
                        <p className="truncate text-[15px] font-semibold tracking-tight text-ink transition-colors duration-300 group-hover:text-apple">
                          {item.brand_name} {item.name}
                        </p>
                        <p className="mt-0.5 text-xs text-ash">
                          {item.kind === "series" ? "车型系列" : "具体 SKU"}
                        </p>
                      </div>
                    </div>
                    <span className="text-gradient ml-3 shrink-0 text-[15px] font-semibold tracking-tight">
                      {item.price_cny != null ? formatPrice(item.price_cny) : "暂无"}
                    </span>
                  </Link>
                </Reveal>
              );
            })}
          </div>
        )}
        {token && (
          <Reveal className="mt-12 border-t border-black/[0.06] pt-5 text-center" delay={60}>
            <button
              type="button"
              onClick={onDeleteAccount}
              className="press rounded-full px-3 py-1.5 text-xs text-ash hover:bg-red-50 hover:text-red-500"
            >
              注销账号（不可恢复）
            </button>
          </Reveal>
        )}
      </main>
    </div>
  );
}
