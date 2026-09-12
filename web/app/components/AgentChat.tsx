"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { createAgentSession, sendAgentMessage, subscribeAgentStream } from "@/lib/agent";
import {
  ENERGY_LABELS,
  formatPrice,
  type AgentMessageOut,
  type Clarification,
  type RecommendedVariant,
} from "@/lib/api";
import { readCompareIds, writeCompareIds } from "./CompareBar";
import { AGENT_ASK_EVENT } from "@/lib/agentTriggers";

interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  payload?: AgentMessageOut;
}

/** 一次性引导气泡的「已关闭」标记（localStorage；读不到时按未关闭处理）。 */
const NUDGE_KEY = "carsel:agent-nudge-dismissed";

/** 车 + 对话气泡：一眼看出「聊着天帮你选车」，避免机器人头像的意图歧义。 */
function CarChatIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden className={className}>
      <g transform="translate(-1.5 2.6) scale(0.86)" fill="currentColor">
        <path d="M18.92 6.01C18.72 5.42 18.16 5 17.5 5h-11c-.66 0-1.21.42-1.42 1.01L3 12v7.5c0 .83.67 1.5 1.5 1.5S6 20.33 6 19.5V19h12v.5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5V12l-2.08-5.99ZM7.5 16.5A1.5 1.5 0 1 1 9 15a1.5 1.5 0 0 1-1.5 1.5Zm9 0a1.5 1.5 0 1 1 1.5-1.5 1.5 1.5 0 0 1-1.5 1.5ZM5.81 10l1.04-3h10.3l1.04 3H5.81Z" />
      </g>
      <g transform="translate(17.3 6.1) scale(0.86) translate(-17.6 -6.6)">
        <path
          d="M17.6 1.4c-3.2 0-5.8 2.3-5.8 5.2 0 2.9 2.6 5.2 5.8 5.2.6 0 1.2-.1 1.7-.2l2.7 1.2-.6-2.3c1.2-.9 2-2.3 2-3.9 0-2.9-2.6-5.2-5.8-5.2Z"
          fill="currentColor"
          stroke="#fff"
          strokeWidth="1.6"
        />
      </g>
    </svg>
  );
}

/**
 * 悬浮购车助手。
 * 低打扰：默认只显示悬浮按钮，点击后才展开聊天窗；绝不自动弹出、不遮挡核心信息。
 * 视觉：Liquid Glass 材质 + iMessage 式气泡 + 弹簧动效（Apple 风格）。
 */
export default function AgentChat() {
  const [open, setOpen] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nudge, setNudge] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  // 页面触发（§12.1：对比页「帮我分析差异」、筛选反复清空等）：展开聊天窗并发起消息。
  // send 依赖会话状态，用 ref 保持监听器始终拿到最新闭包。
  const sendRef = useRef(send);
  sendRef.current = send;

  function dismissNudge() {
    setNudge(false);
    try {
      window.localStorage.setItem(NUDGE_KEY, "1");
    } catch {
      /* 隐私模式等禁用 localStorage：忽略，下次访问再提示 */
    }
  }

  // 首次访问延迟浮出一次引导气泡（关闭过就不再出现）；「低打扰」：不自动展开聊天窗
  useEffect(() => {
    let dismissed = false;
    try {
      dismissed = window.localStorage.getItem(NUDGE_KEY) === "1";
    } catch {
      dismissed = false;
    }
    if (dismissed) return;
    const show = window.setTimeout(() => setNudge(true), 1800);
    const hide = window.setTimeout(() => setNudge(false), 16000);
    return () => {
      window.clearTimeout(show);
      window.clearTimeout(hide);
    };
  }, []);

  useEffect(() => {
    const onAsk = (e: Event) => {
      const text = (e as CustomEvent<{ text?: string }>).detail?.text?.trim();
      if (!text) return;
      dismissNudge();
      setOpen(true);
      window.setTimeout(() => sendRef.current(text), 60);
    };
    window.addEventListener(AGENT_ASK_EVENT, onAsk);
    return () => window.removeEventListener(AGENT_ASK_EVENT, onAsk);
  }, []);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    setError(null);
    setMessages((prev) => [...prev, { role: "user", text: trimmed }]);
    setInput("");
    setBusy(true);
    try {
      const sid = sessionId ?? (await createAgentSession());
      setSessionId(sid);
      const payload = await sendAgentMessage(sid, trimmed);
      const assistantIndex = messages.length + 1;
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: payload.explanation ?? "", payload },
      ]);
      // SSE 为权威事件源（生产经 Redis Stream 桥接）；失败时保留 POST 结果
      const controller = new AbortController();
      setTimeout(() => controller.abort(), 3000);
      subscribeAgentStream(
        sid,
        (fresh) => {
          setMessages((prev) =>
            prev.map((m, i) =>
              i === assistantIndex
                ? { role: "assistant" as const, text: fresh.explanation ?? "", payload: fresh }
                : m,
            ),
          );
        },
        controller.signal,
      ).catch(() => {});
    } catch (err) {
      setError(err instanceof Error ? err.message : "网络错误，请稍后重试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed bottom-6 right-6 z-40 flex flex-col items-end gap-3">
      {open && (
        <div className="glass-strong animate-scale-in flex h-[min(540px,calc(100dvh-7rem))] w-[380px] max-w-[calc(100vw-3rem)] origin-bottom-right flex-col overflow-hidden rounded-[28px] border border-black/[0.07]">
          {/* 头部：磨砂白 + AI 徽标 */}
          <div className="flex items-center justify-between border-b border-black/[0.06] bg-white/60 px-4 py-3 backdrop-blur-xl">
            <div className="flex items-center gap-2.5">
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-[#0a84ff] via-apple to-[#5e5ce6] text-white shadow-md shadow-apple/30">
                <CarChatIcon className="h-6 w-6" />
              </span>
              <div>
                <p className="flex items-center gap-1.5 text-[15px] font-semibold tracking-tight text-ink">
                  帮我选车
                  <span className="rounded-full bg-apple/10 px-1.5 py-px text-[10px] font-bold text-apple">AI 导购</span>
                </p>
                <p className="text-[11px] leading-4 text-ash">说出预算与用途 · 真实车型数据 · 带来源引用</p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="关闭"
              className="press flex h-7 w-7 items-center justify-center rounded-full bg-black/[0.06] text-xs text-ash hover:bg-black/10 hover:text-ink"
            >
              ✕
            </button>
          </div>

          {/* 消息区 */}
          <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto bg-canvas/40 px-4 py-4">
            {messages.length === 0 && (
              <div className="msg-pop rounded-[20px] border border-apple/12 bg-ice/70 p-3.5 text-[13px] leading-6 text-ink-soft">
                💡 告诉我你的预算、用途和人数，例如：「预算15万，家庭用车，5口人，想要新能源SUV」。
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={m.role === "user" ? "text-right" : "text-left"}>
                <div
                  className={`msg-pop inline-block max-w-[85%] whitespace-pre-wrap px-3.5 py-2.5 text-left text-[13.5px] leading-6 ${
                    m.role === "user"
                      ? "rounded-[22px] rounded-br-md bg-gradient-to-b from-[#3b9bff] to-apple text-white shadow-sm shadow-apple/30"
                      : "rounded-[22px] rounded-bl-md bg-[#e9e9ee]/90 text-ink shadow-sm shadow-black/[0.03]"
                  }`}
                >
                  {m.text}
                </div>
                {m.role === "assistant" && m.payload && (
                  <AgentExtras payload={m.payload} onPick={(option) => send(option)} />
                )}
              </div>
            ))}
            {busy && (
              <div className="msg-pop inline-flex items-center gap-1.5 rounded-[22px] rounded-bl-md bg-[#e9e9ee]/90 px-4 py-3 shadow-sm">
                {[0, 1, 2].map((i) => (
                  <span
                    key={i}
                    className="typing-dot h-1.5 w-1.5 rounded-full bg-ash"
                    style={{ animationDelay: `${i * 0.15}s` }}
                  />
                ))}
                <span className="ml-1.5 text-[11px] text-ash">正在筛选车型…</span>
              </div>
            )}
            {error && (
              <div className="msg-pop rounded-2xl border border-red-200/60 bg-red-50/90 p-2.5 text-xs text-red-600">
                {error}
              </div>
            )}
          </div>

          {/* 输入区：iMessage 式圆形发送键 */}
          <div className="border-t border-black/[0.06] bg-white/60 p-3 backdrop-blur-xl">
            <div className="flex items-center gap-2">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.nativeEvent.isComposing) send(input);
                }}
                placeholder="描述你的购车需求…"
                className="min-w-0 flex-1 rounded-full border border-transparent bg-canvas px-4 py-2.5 text-[13.5px] text-ink transition-all duration-300 placeholder:text-ash/70 focus:border-apple/50 focus:bg-white focus:outline-none focus:ring-4 focus:ring-apple/12"
              />
              <button
                type="button"
                onClick={() => send(input)}
                disabled={busy || !input.trim()}
                aria-label="发送"
                className="press flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-apple text-white shadow-md shadow-apple/30 hover:bg-[#0077ed] disabled:opacity-35 disabled:shadow-none"
              >
                <svg viewBox="0 0 20 20" fill="currentColor" className="h-[18px] w-[18px]">
                  <path
                    fillRule="evenodd"
                    d="M10 17a.75.75 0 0 1-.75-.75V5.61L5.53 9.33a.75.75 0 1 1-1.06-1.06l5-5a.75.75 0 0 1 1.06 0l5 5a.75.75 0 1 1-1.06 1.06l-3.72-3.72v10.64A.75.75 0 0 1 10 17Z"
                    clipRule="evenodd"
                  />
                </svg>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 一次性引导气泡：只在没关闭过时出现，且不自动打开聊天窗（低打扰） */}
      {nudge && !open && (
        <div className="glass-strong animate-scale-in relative max-w-[268px] origin-bottom-right rounded-[22px] rounded-br-md border border-black/[0.07] p-4 text-left shadow-[0_18px_40px_-16px_rgba(0,0,0,0.28)]">
          <button
            type="button"
            onClick={dismissNudge}
            aria-label="不再提示"
            className="press absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-full text-[11px] text-ash hover:bg-black/[0.06] hover:text-ink"
          >
            ✕
          </button>
          <p className="flex items-center gap-1.5 text-[13.5px] font-semibold tracking-tight text-ink">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-gradient-to-br from-[#0a84ff] via-apple to-[#5e5ce6] text-white">
              <CarChatIcon className="h-4 w-4" />
            </span>
            买车拿不定主意？
          </p>
          <p className="mt-1.5 pr-4 text-xs leading-5 text-ink-soft">
            说说预算和用途，例如「预算15万，家用5口人，想要新能源SUV」，我按真实车型数据帮你挑 3 款。
          </p>
          <div className="mt-2.5 flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                dismissNudge();
                setOpen(true);
              }}
              className="press rounded-full bg-apple px-3.5 py-1.5 text-xs font-medium text-white shadow-sm shadow-apple/30 hover:bg-[#0077ed]"
            >
              帮我选车 →
            </button>
            <span className="text-[11px] text-ash">免费 · 不用注册</span>
          </div>
        </div>
      )}

      <button
        type="button"
        onClick={() => {
          if (!open) dismissNudge();
          setOpen((v) => !v);
        }}
        aria-label={open ? "收起购车助手" : "打开购车助手：帮我选车"}
        aria-expanded={open}
        className="animate-glow group flex h-14 items-center gap-2.5 rounded-full bg-gradient-to-br from-[#0a84ff] via-apple to-[#5e5ce6] px-4 text-white transition-transform duration-500 ease-[cubic-bezier(0.34,1.56,0.64,1)] hover:scale-105 active:scale-95 sm:px-5"
      >
        {open ? (
          <>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" className="msg-pop h-5 w-5">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
            <span className="hidden text-[15px] font-semibold tracking-tight sm:block">收起</span>
          </>
        ) : (
          <>
            <CarChatIcon className="msg-pop h-7 w-7 transition-transform duration-500 group-hover:rotate-6" />
            <span className="hidden text-[15px] font-semibold tracking-tight sm:block">帮我选车</span>
            <span className="hidden rounded-full bg-white/22 px-1.5 py-px text-[10px] font-bold tracking-wide sm:block">
              AI 导购
            </span>
          </>
        )}
      </button>
    </div>
  );
}

function AgentExtras({
  payload,
  onPick,
}: {
  payload: AgentMessageOut;
  onPick: (option: string) => void;
}) {
  if (payload.need_clarification && payload.clarification) {
    return <ClarificationChips clarification={payload.clarification} onPick={onPick} />;
  }
  if (payload.recommended_variants.length === 0) return null;
  return (
    <div className="mt-2 space-y-2">
      {payload.recommended_variants.map((v) => (
        <RecommendationCard key={v.variant_id} variant={v} />
      ))}
      {payload.citations.length > 0 && (
        <p className="text-left text-[11px] leading-5 text-ash">
          来源：
          {payload.citations
            .map((c) => c.source_name ?? `#${c.source_id}`)
            .filter((v, i, a) => a.indexOf(v) === i)
            .join("、")}
        </p>
      )}
    </div>
  );
}

function ClarificationChips({
  clarification,
  onPick,
}: {
  clarification: Clarification;
  onPick: (option: string) => void;
}) {
  return (
    <div className="msg-pop mt-2 space-y-2 text-left">
      <p className="text-xs leading-5 text-ash">{clarification.question}</p>
      <div className="flex flex-wrap gap-1.5">
        {clarification.options.map((opt) => (
          <button
            key={opt}
            type="button"
            onClick={() => onPick(opt)}
            className="press rounded-full border border-apple/25 bg-white/90 px-3 py-1.5 text-xs font-medium text-apple shadow-sm hover:bg-ice"
          >
            {opt}
          </button>
        ))}
      </div>
    </div>
  );
}

function RecommendationCard({ variant }: { variant: RecommendedVariant }) {
  return (
    <div className="lift msg-pop rounded-[20px] border border-black/[0.06] bg-white/95 p-3.5 text-left shadow-sm">
      <Link
        href={`/vehicles/${variant.series_id}`}
        className="text-[13.5px] font-semibold tracking-tight text-ink transition-colors duration-300 hover:text-apple"
      >
        {variant.brand_name} {variant.series_name} · {variant.display_name}
      </Link>
      <p className="mt-1 flex flex-wrap items-baseline gap-x-2">
        <span className="text-gradient text-[17px] font-semibold tracking-tight">
          {variant.price_cny != null ? formatPrice(variant.price_cny) : "官方资料未披露"}
        </span>
        <span className="text-[11px] text-ash">
          {ENERGY_LABELS[variant.energy_type] ?? variant.energy_type}
          {/* score=0 表示非评分场景（如「同车系版本对比」卡片），不显示匹配分 */}
          {variant.score > 0 ? ` · 匹配分 ${variant.score.toFixed(2)}` : ""}
        </span>
      </p>
      {variant.matched.length > 0 && (
        <p className="mt-1.5 text-xs leading-5 text-[#1d7d3f]">{variant.matched.join("、")}</p>
      )}
      {/* 内部说明类妥协项（未参与评分/暂无数据源等）不呈现给用户 */}
      {(() => {
        const internalNotes = ["未参与", "暂无", "未披露", "数据源"];
        const clean = (variant.tradeoffs ?? []).filter(
          (t) => !internalNotes.some((note) => t.includes(note)),
        );
        return clean.length > 0 ? (
          <p className="mt-1 text-xs leading-5 text-[#b25e09]">妥协项：{clean.slice(0, 2).join("；")}</p>
        ) : null;
      })()}
      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <Link
          href={`/vehicles/${variant.series_id}`}
          className="press rounded-full border border-black/[0.08] bg-white px-3 py-1 text-xs text-ink-soft hover:border-apple/35 hover:text-apple"
        >
          查看详情
        </Link>
        <AddFromAgent variantId={variant.variant_id} />
        {variant.official_page_url && (
          <a
            href={variant.official_page_url}
            target="_blank"
            rel="noopener noreferrer"
            className="press rounded-full border border-black/[0.08] bg-white px-3 py-1 text-xs text-ink-soft hover:border-apple/35 hover:text-apple"
          >
            官方车型页 ↗
          </a>
        )}
      </div>
    </div>
  );
}

function AddFromAgent({ variantId }: { variantId: number }) {
  const [added, setAdded] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        const ids = readCompareIds();
        if (ids.includes(variantId)) {
          setAdded(true);
          return;
        }
        if (ids.length >= 5) {
          window.alert("最多同时对比 5 个 SKU，请先移除部分车型。");
          return;
        }
        writeCompareIds([...ids, variantId]);
        setAdded(true);
      }}
      className={`press rounded-full px-3 py-1 text-xs font-medium ${
        added
          ? "bg-apple text-white shadow-sm shadow-apple/30"
          : "border border-apple/25 bg-white text-apple hover:bg-ice"
      }`}
    >
      {added ? "已加入对比 ✓" : "加入对比"}
    </button>
  );
}
