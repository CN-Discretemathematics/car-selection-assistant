import type { AgentMessageOut } from "@/lib/api";

export async function createAgentSession(): Promise<string> {
  const res = await fetch("/api/v1/agent/sessions", { method: "POST" });
  if (!res.ok) throw new Error(`创建会话失败（${res.status}）`);
  const body = await res.json();
  return body.session_id as string;
}

export async function sendAgentMessage(
  sessionId: string,
  message: string,
): Promise<AgentMessageOut> {
  const res = await fetch(`/api/v1/agent/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `请求失败（${res.status}）`);
  }
  return res.json() as Promise<AgentMessageOut>;
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
