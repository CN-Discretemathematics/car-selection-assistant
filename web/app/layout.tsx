import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import AgentChat from "./components/AgentChat";
import CompareBar from "./components/CompareBar";

export const metadata: Metadata = {
  title: "选车助手 | 家用新车推荐",
  description:
    "浏览车型、查看详情、SKU 级对比。按月销量排序，只展示官方指导价，全部数据带来源与更新时间。",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen pb-24">
        {children}
        <footer className="mt-16 border-t border-black/[0.07] bg-white/55 py-8 text-center text-xs leading-6 text-ash backdrop-blur-xl">
          <p className="mx-auto max-w-3xl px-4">
            <a href="/privacy" className="text-ash underline-offset-4 transition hover:text-apple hover:underline">
              隐私政策
            </a>
            <span className="mx-2 text-hair">|</span>
            数据均带来源与更新时间
            <span className="mx-2 text-hair">|</span>
            购车助手内容由 AI 生成，仅供参考
          </p>
          <p className="mt-2 px-4 text-ash/80">本站不提供站内交易入口；价格与配置以品牌官网为准。</p>
          {/* 备案号：备案通过后由环境变量注入 */}
          {process.env.ICP_NUMBER && <p className="mt-2">{process.env.ICP_NUMBER}</p>}
        </footer>
        <CompareBar />
        <AgentChat />
      </body>
    </html>
  );
}
