import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import AgentChat from "./components/AgentChat";
import CompareBar from "./components/CompareBar";

export const metadata: Metadata = {
  title: "选车助手 | 家用新车推荐",
  description:
    "浏览车型、查看详情、款型级对比。按月销量排序，只展示官方指导价，全部数据带来源与更新时间。",
};

// 浅色主题下让移动端浏览器状态栏/边框与画布同色（Web Interface Guidelines: theme-color）
export const viewport: Viewport = {
  themeColor: "#f5f5f7",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen pb-24">
        {/* 跳转链接：键盘用户可直达主内容（Web Interface Guidelines: skip link） */}
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-full focus:bg-apple focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-white focus:shadow-lg"
        >
          跳到主要内容
        </a>
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
          {/* 备案号：备案通过后由环境变量注入；按规范须链接到工信部备案系统 */}
          {process.env.ICP_NUMBER && (
            <p className="mt-2">
              <a
                href="https://beian.miit.gov.cn/"
                target="_blank"
                rel="noreferrer"
                className="underline-offset-4 transition hover:text-apple hover:underline"
              >
                {process.env.ICP_NUMBER}
              </a>
            </p>
          )}
        </footer>
        <CompareBar />
        <AgentChat />
      </body>
    </html>
  );
}
