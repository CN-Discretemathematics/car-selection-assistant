/** Agent 介入触发器（筛选反复清空 / 页面主动求助）。 */

export const AGENT_ASK_EVENT = "agent:ask";

/** 让悬浮 Agent 收到一段消息并展开聊天窗（详情页/对比页/筛选触发共用）。 */
export function askAgent(text: string): void {
  window.dispatchEvent(new CustomEvent(AGENT_ASK_EVENT, { detail: { text } }));
}

const CLEAR_WINDOW_MS = 2 * 60 * 1000; // 2 分钟
const CLEAR_THRESHOLD = 3; // ≥3 次清空

/**
 * §12.1「筛选器反复清空（2 分钟内 ≥3 次）」→ 自动请 Agent 介入。
 * 计数存 sessionStorage（按页面上下文隔离），达到阈值即触发并清零。
 */
export function trackFilterClear(context: string, askText: string): void {
  const key = `filter-clear:${context}`;
  const now = Date.now();
  let stamps: number[] = [];
  try {
    stamps = JSON.parse(window.sessionStorage.getItem(key) ?? "[]") as number[];
  } catch {
    stamps = [];
  }
  const recent = stamps.filter((t) => now - t < CLEAR_WINDOW_MS);
  recent.push(now);
  window.sessionStorage.setItem(key, JSON.stringify(recent));
  if (recent.length >= CLEAR_THRESHOLD) {
    window.sessionStorage.removeItem(key);
    askAgent(askText);
  }
}
