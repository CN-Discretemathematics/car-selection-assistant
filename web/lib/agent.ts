import type { AgentMessageOut } from "@/lib/api";

/**
 * 匿名身份 token 的本地存储键（**跨会话记忆的载体**）。
 *
 * 为什么要存在：后端把跨会话记忆挂在**匿名身份**上——请求不带身份时
 * `identity=None`，记忆既不写也不读（行为与接入前逐位相同）。
 * 此前前端从不领身份，于是「偏好记忆 / 拒绝反馈 / 账户合并」整套能力
 * 在产品上**不可达**（2026-10-21 生产实况核查：`anonymous_identities` 长期 0 行）。
 */
const IDENTITY_KEY = "carsel:agent-identity-token";

export function readIdentityToken(): string | null {
  if (typeof window === "undefined") return null;
  // 读取也要 try/catch：Safari 无痕模式 / 禁用 Cookie / 存储配额异常时
  // `getItem` 会**抛异常**（写法对齐 lib/auth.ts 的 getToken）。
  try {
    return window.localStorage.getItem(IDENTITY_KEY);
  } catch {
    return null;
  }
}

export function writeIdentityToken(token: string | null) {
  if (typeof window === "undefined") return;
  try {
    if (token) window.localStorage.setItem(IDENTITY_KEY, token);
    else window.localStorage.removeItem(IDENTITY_KEY);
  } catch {
    /* 隐私模式等禁用 localStorage：忽略（没有身份 = 没有跨会话记忆，问车照常） */
  }
}

/** 领身份请求的**在途** Promise：并发时只发一次，避免凭空多出几个身份。 */
let inflightIdentity: Promise<string | null> | null = null;

/**
 * 领一个匿名身份；已持有则直接复用。
 *
 * 返回 `null` 表示**没领到**（网络失败 / 被禁用存储 / 服务端报错）——
 * 此时按「无身份」发消息，后端照常回答（缺身份只少跨会话记忆，不影响问车）。
 * 这个降级是刻意的：身份是增强项，**不能让它把主功能带挂**。
 */
export async function ensureIdentityToken(): Promise<string | null> {
  const existing = readIdentityToken();
  if (existing) return existing;
  inflightIdentity ??= (async () => {
    try {
      const res = await fetch("/api/v1/agent/identity", { method: "POST" });
      if (!res.ok) return null;
      const body = await res.json();
      // 明文 token **只在新建时给一次**（库里只存哈希）；已存在时后端返回空串。
      const token = typeof body?.token === "string" ? body.token : "";
      if (token) writeIdentityToken(token);
      return token || null;
    } catch {
      return null;
    } finally {
      inflightIdentity = null;
    }
  })();
  return inflightIdentity;
}

export async function createAgentSession(): Promise<string> {
  const res = await fetch("/api/v1/agent/sessions", { method: "POST" });
  if (!res.ok) throw new Error(`创建会话失败（${res.status}）`);
  const body = await res.json();
  return body.session_id as string;
}

export async function sendAgentMessage(
  sessionId: string,
  message: string,
  identityToken?: string | null,
): Promise<AgentMessageOut> {
  const res = await fetch(`/api/v1/agent/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // 带身份 = 这一轮的偏好会**跨会话**被记住（预算 / 用途 / 排除项 / 拒绝）。
      // 没有身份也能问——后端对缺身份的请求不报错，只是没有跨会话记忆。
      ...(identityToken ? { Authorization: `Bearer ${identityToken}` } : {}),
    },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `请求失败（${res.status}）`);
  }
  return res.json() as Promise<AgentMessageOut>;
}

/**
 * 清空该会话的记忆（后端删除画像/消息/上次结果），会话 ID 继续可用。
 * 用于「重新开始这段对话」：Agent 的约束在会话内累积，理解错了需要有正式入口重置。
 */
export async function resetAgentSession(sessionId: string): Promise<void> {
  const res = await fetch(`/api/v1/agent/sessions/${sessionId}/reset`, { method: "POST" });
  // 404 = 会话已过期（Redis TTL 到期）；对调用方等价于「已经是干净的」，不报错
  if (!res.ok && res.status !== 404) throw new Error(`重置会话失败（${res.status}）`);
}

/**
 * 订阅会话 SSE 流（后端 /stream 端点；生产环境由 Redis Stream 事件中转桥接）。
 * 返回的 payload 与 POST 结果同构，作为权威事件源；SSE 不可用时调用方回退 POST 响应。
 */
export async function subscribeAgentStream(
  sessionId: string,
  onMessage: (payload: AgentMessageOut) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api/v1/agent/sessions/${sessionId}/stream`, { signal });
  if (!res.ok || !res.body) throw new Error(`SSE 连接失败（${res.status}）`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const block of events) {
      const eventLine = block.split("\n").find((l) => l.startsWith("event:"));
      const dataLine = block.split("\n").find((l) => l.startsWith("data:"));
      if (eventLine?.slice(6).trim() === "message" && dataLine) {
        onMessage(JSON.parse(dataLine.slice(5).trim()) as AgentMessageOut);
      }
    }
  }
}
