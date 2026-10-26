import type { AgentMessageOut, PreferenceMemory } from "@/lib/api";

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
 * 后端「这个 token 不认识」的信号。
 *
 * 2026-10-24 生产实测：浏览器里存着一个**已被删除**的身份 token
 * （服务端清了 `anonymous_identities`）⇒ 后端 `get_identity_by_token` 返回 None
 * ⇒ 身份为 None ⇒ 记忆读写**全部短路**、且**不报任何错**：
 *   - 「我记住的偏好」面板显示「你什么也没被记住」——**那是谎**
 *     （真相是「读不到」，两件事在 UI 上长得一模一样）；
 *   - 后续每一轮问车都**不写记忆** ⇒ 整套跨会话记忆能力对用户静默失效。
 *
 * ⇒ 前端不能「本地有 token 就直接复用」——必须**问一次服务端**它认不认。
 */
async function identityIsKnown(token: string): Promise<boolean> {
  try {
    // 用**领身份接口**探活：带已存在的 token 请求它，服务端认 ⇒
    // 返回 created=false + 空 token；**不认识** ⇒ 新建一个并把新 token 给我们
    // （那正是我们要的「旧 token 失效 → 换新身份」）。
    const res = await fetch("/api/v1/agent/identity", { method: "POST" });
    if (!res.ok) {
      // 服务端明确报错（5xx）= 「我处理不了」，**不是**「这个 token 不认」。
      // ⇒ 不能据此判定失效（那会把好 token 丢掉），也不能当有效。
      //    保守返回 false ⇒ 本轮不用这个 token（照常问车，只是不带跨会话记忆）。
      return false;
    }
    const body = await res.json();
    const fresh = typeof body?.token === "string" ? body.token : "";
    if (fresh) {
      // 服务端**不认识**旧 token ⇒ 给了新身份 ⇒ 换掉本地这个孤儿
      writeIdentityToken(fresh);
      return false;
    }
    // 服务端认这个 token（created=false、token 为空串）⇒ 有效
    return true;
  } catch {
    return true; // 网络故障：不能据此判定失效（否则会把好 token 丢掉）
  }
}

/**
 * 领一个匿名身份；已持有则**先验证**再复用。
 *
 * 返回 `null` 表示**没领到**（网络失败 / 被禁用存储 / 服务端报错）——
 * 此时按「无身份」发消息，后端照常回答（缺身份只少跨会话记忆，不影响问车）。
 * 这个降级是刻意的：身份是增强项，**不能让它把主功能带挂**。
 */
export async function ensureIdentityToken(): Promise<string | null> {
  const existing = readIdentityToken();
  if (existing) {
    // ⚠️ 不能直接复用：本地存的 token 可能**已经被服务端删了**
    // （服务端清库 / 用户注销 / 换了环境）⇒ 复用一个死 token 会让
    // 整套跨会话记忆**静默失效**（2026-10-24 生产实测）。
    // ⇒ 先问一次服务端它认不认：
    //    认 ⇒ 复用；不认（服务端给了新 token）⇒ 换成新的；
    //    探活本身失败（5xx / 网络）⇒ **本轮不用**它（问车照常，只是少跨会话记忆），
    //    但**不删**本地 token（网络抖动不该把唯一凭据丢掉）。
    const known = await identityIsKnown(existing);
    if (known) return existing;
    // identityIsKnown 在「服务端不认」时已把新 token 写进本地 ⇒ 这里读到的就是新的；
    // 在「探活失败」时读到的仍是旧的 ⇒ 那种情况下**不用**它（返回 null）。
    const fresh = readIdentityToken();
    return fresh === existing ? null : fresh;
  }
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
): Promise<AgentMessageOut> {  const res = await fetch(`/api/v1/agent/sessions/${sessionId}/messages`, {
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

/**
 * 列出这个浏览器**已经记住**的偏好（用户自查用）。
 *
 * 后端接口：`GET /api/v1/agent/preferences`——未带身份或身份无效时返回**空列表**
 * （不报错），所以不带 token 也可以调用。
 *
 * ⚠️ 失败**抛错**而不是返回空列表：空列表会被 UI 读成「你什么也没被记住」，
 * 而那是个**谎**（读不到 ≠ 没有）。由调用方区分这两种情况。
 */
export async function listPreferences(
  identityToken?: string | null,
): Promise<{ items: PreferenceMemory[]; identityValid: boolean }> {
  const res = await fetch("/api/v1/agent/preferences", {
    headers: identityToken ? { Authorization: `Bearer ${identityToken}` } : {},
  });
  if (!res.ok) throw new Error(`读取已记住的偏好失败（${res.status}）`);
  const body = await res.json();
  const items: PreferenceMemory[] = Array.isArray(body) ? (body as PreferenceMemory[]) : [];

  // 带了 token 却一条都没有 ⇒ 分不清「真的没记住」还是「token 已失效」⇒ 探一次。
  // 后端对两者都返回空列表（200），UI 上长得一模一样 ⇒ 真相会被读成谎。
  let identityValid = true;
  if (identityToken && items.length === 0) {
    identityValid = await identityIsKnown(identityToken);
  }
  return { items, identityValid };
}

/**
 * 撤回一条记忆（用户「我不想让你记这个」）。
 *
 * 后端接口：`DELETE /api/v1/agent/preferences/{memory_id}`（204）。
 * 撤回后**下一次**会话起不再生效（当前会话里已经说过的话不受影响）。
 */
export async function deletePreference(
  memoryId: number,
  identityToken?: string | null,
): Promise<void> {
  const res = await fetch(`/api/v1/agent/preferences/${memoryId}`, {
    method: "DELETE",
    headers: identityToken ? { Authorization: `Bearer ${identityToken}` } : {},
  });
  if (!res.ok) throw new Error(`撤回失败（${res.status}）`);
}
