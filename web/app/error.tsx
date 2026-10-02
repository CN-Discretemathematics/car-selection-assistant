"use client";

import Link from "next/link";
import { useEffect } from "react";

/**
 * 全站错误边界（2026-10-03 新增）。
 *
 * 此前 `web/app` 下**没有任何** error/loading/not-found 文件，于是任何服务端
 * 渲染出错都会落到框架自带的通用错误页：站点外壳（页脚、对话入口、对比栏）
 * 全部消失，页面上只剩一句技术性提示，**用户没有任何可点的恢复入口**。
 *
 * 放在根 segment 的原因：它兜住的是所有页面，而 `RootLayout` 渲染的是
 * `{children}` + 页脚，边界只替换 children，**外壳仍然保留**——用户不会从
 * 「有导航的站点」掉进「一张白纸」。
 *
 * 两条自我约束：
 * 1. **不显示 `error.message`**。它可能含内部路径/键名/上游报错原文，是给排障
 *    看的，不是给用户看的。要留痕就用 `useEffect` 打到控制台（失败要显式）。
 * 2. **digest 对用户可见**。它是框架生成的**不透明**编号，不泄露任何实现细节，
 *    但用户报障时报它能把一次具体故障对上服务端日志——这比让用户截图整页有用。
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // 排障线索只进控制台，不进页面文案
    console.error("[page error]", error);
  }, [error]);

  return (
    <main id="main-content" className="mx-auto flex min-h-[60vh] max-w-2xl flex-col items-center justify-center px-6 text-center">
      <p role="alert" className="text-sm font-medium text-apple">
        页面没能正常显示
      </p>
      <h1 className="mt-3 text-2xl font-semibold text-ink">内容暂时取不到</h1>
      <p className="mt-3 text-sm leading-6 text-ash">
        这通常是临时的。你可以重试一次；如果仍然不行，回到首页重新开始。
      </p>
      {error.digest ? (
        <p className="mt-4 font-mono text-xs text-hair">问题编号：{error.digest}</p>
      ) : null}
      <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
        <button
          type="button"
          onClick={reset}
          className="press rounded-full bg-apple px-5 py-2 text-sm font-medium text-white shadow-sm shadow-apple/25 transition hover:bg-[#0077ed]"
        >
          重试
        </button>
        <Link
          href="/"
          className="press rounded-full border border-black/[0.08] bg-white/85 px-5 py-2 text-sm font-medium text-ink-soft transition hover:border-apple/35 hover:text-apple"
        >
          返回首页
        </Link>
      </div>
    </main>
  );
}
