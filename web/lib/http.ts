/**
 * 前端共用的 fetch 封装（2026-10-02 抽取）。
 *
 * **为什么抽**
 * 同一段「取 JSON → 判 ok → 抛错」此前在 `lib/auth.ts`（post / request 两份）、
 * `lib/rag.ts`（request）、`app/compare/page.tsx` 共 4 处逐字重复：
 *
 *     const body = await res.json().catch(() => null);
 *     if (!res.ok) throw new Error(body?.detail ?? `请求失败（${res.status}）`);
 *
 * 重复本身不是问题，**问题在于这 4 份都缺少同一个守卫**：
 * `body` 来自 `.json().catch(() => null)`，类型是 `any`；而后端 `detail`
 * 并非总是字符串——FastAPI 的 `RequestValidationError`（422）返回的是
 * **对象数组**。于是 `new Error(detailArray)` 的 message 会变成
 * `"[object Object]"`；在 compare 页更糟：`setError(body?.detail)` 把它
 * 当 React child 渲染，数组/对象会直接抛
 * `Objects are not valid as a React child` **整页白屏**。
 * 收进一个函数后，类型在这里被收窄一次，4 个调用点同时获得保护。
 *
 * **行为承诺**：detail 是字符串时，与原实现**逐字一致**（同一段文案、同一优先级）。
 * 仅在 detail 非字符串时才走新分支——那原本是 bug 路径，不是行为。
 */

/** 从后端错误体里取一句人类可读的说明；取不到就给调用方的兜底文案。 */
export function extractErrorDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    // 只有字符串才是可直接展示的文案。FastAPI 422 的 detail 是对象数组。
    if (typeof detail === "string" && detail) return detail;
  }
  return fallback;
}

/**
 * 发起请求并解析 JSON。
 *
 * @param input    fetch 的第一个参数
 * @param init     fetch 选项
 * @param onError  失败时写入 `error` state 的回调（可为 null：调用方自己 catch）
 */
export async function requestJson<T>(
  input: RequestInfo | URL,
  init: RequestInit = {},
  onError?: ((message: string) => void) | null,
): Promise<T> {
  const res = await fetch(input, init);
  // 解析失败也走 catch：后端返回 HTML 错误页时 json() 会抛，不能让它盖掉状态码文案
  const body = (await res.json().catch(() => null)) as T | null;
  if (!res.ok) {
    const message = extractErrorDetail(body, `请求失败（${res.status}）`);
    if (onError) onError(message);
    throw new Error(message);
  }
  return body as T;
}

/** JSON POST：带 `Content-Type`，可选 Bearer 令牌。 */
export function postJson<T>(
  path: string,
  payload: unknown,
  token?: string,
): Promise<T> {
  return requestJson<T>(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
  });
}

/** 带 Bearer 令牌的 JSON 请求（管理端 / 用户端共用）。 */
export function authedJson<T>(
  path: string,
  init: RequestInit,
  token: string,
  basePath = "",
): Promise<T> {
  return requestJson<T>(`${basePath}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
  });
}
