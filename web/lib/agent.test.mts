// 前端匿名身份接线的回归测试（零依赖：Node 22 内置 test runner + 类型剥离）。
//
// 为什么是 .mts：node 的 ESM 解析要求导入路径带扩展名（./agent.ts），
// 而 TS 默认禁止 .ts 扩展名导入（TS5097）。写法对齐同目录的 api.test.mts。
//
// 运行：pnpm test（= node --experimental-strip-types --test "lib/**/*.test.mts"）
//
// 背景：跨会话记忆挂在**匿名身份**上，而后端对缺身份的请求不报错
// （`identity=None` ⇒ 记忆不写不读）。这意味着**前端不接线，记忆整套能力
// 就在产品上不可达，而线上没有任何报错**——2026-10-21 生产实况核查就是这样
// 发现的：`anonymous_identities` 长期 0 行。所以这几条用例钉的是「接线在」。

import test, { afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  ensureIdentityToken,
  readIdentityToken,
  deletePreference,
  listPreferences,
  sendAgentMessage,
  writeIdentityToken,
} from "./agent.ts";

const IDENTITY_KEY = "carsel:agent-identity-token";

type FetchCall = { url: string; init: RequestInit | undefined };

/** 极简 localStorage 桩（node 里没有 window / localStorage）。 */
function stubStorage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  const storage = {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
    removeItem: (k: string) => {
      map.delete(k);
    },
  };
  (globalThis as unknown as { window: unknown }).window = { localStorage: storage };
  return map;
}

/** fetch 桩：记录每次调用，按 handler 返回响应。 */
function stubFetch(
  handler: (url: string, init: RequestInit | undefined) => {
    ok: boolean;
    status: number;
    body: unknown;
  },
) {
  const calls: FetchCall[] = [];
  (globalThis as unknown as { fetch: unknown }).fetch = async (
    url: string,
    init?: RequestInit,
  ) => {
    calls.push({ url: String(url), init });
    const r = handler(String(url), init);
    return {
      ok: r.ok,
      status: r.status,
      json: async () => r.body,
    } as Response;
  };
  return calls;
}

afterEach(() => {
  delete (globalThis as unknown as { window?: unknown }).window;
  delete (globalThis as unknown as { fetch?: unknown }).fetch;
});

test("ensureIdentityToken：已有 token 直接复用，不再发请求", async () => {
  stubStorage({ [IDENTITY_KEY]: "tok-existing" });
  const calls = stubFetch(() => {
    throw new Error("不该发请求");
  });
  assert.equal(await ensureIdentityToken(), "tok-existing");
  assert.equal(calls.length, 0);
});

test("ensureIdentityToken：新建身份后把 token 存进 localStorage", async () => {
  stubStorage();
  const calls = stubFetch(() => ({ ok: true, status: 201, body: { identity_id: 7, token: "tok-new" } }));
  const token = await ensureIdentityToken();
  assert.equal(token, "tok-new");
  assert.equal(readIdentityToken(), "tok-new");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/agent/identity");
  assert.equal(calls[0].init?.method, "POST");
});

test("ensureIdentityToken：并发只发一次请求（不凭空多出几个身份）", async () => {
  stubStorage();
  const calls = stubFetch(() => ({ ok: true, status: 201, body: { identity_id: 7, token: "tok-new" } }));
  const [a, b, c] = await Promise.all([
    ensureIdentityToken(),
    ensureIdentityToken(),
    ensureIdentityToken(),
  ]);
  assert.equal(calls.length, 1);
  assert.equal(a, "tok-new");
  assert.equal(b, "tok-new");
  assert.equal(c, "tok-new");
});

test("ensureIdentityToken：领不到身份时返回 null 而不是抛错（不把问车带挂）", async () => {
  stubStorage();
  stubFetch(() => ({ ok: false, status: 500, body: { detail: "boom" } }));
  assert.equal(await ensureIdentityToken(), null);
  assert.equal(readIdentityToken(), null);
});

test("ensureIdentityToken：fetch 直接抛异常时也只降级不崩", async () => {
  stubStorage();
  (globalThis as unknown as { fetch: unknown }).fetch = async () => {
    throw new Error("network down");
  };
  assert.equal(await ensureIdentityToken(), null);
});

test("sendAgentMessage：带 token 时带 Authorization 头", async () => {
  const calls = stubFetch(() => ({ ok: true, status: 200, body: { explanation: "好" } }));
  await sendAgentMessage("s1", "预算15万", "tok-abc");
  const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
  assert.equal(headers.Authorization, "Bearer tok-abc");
  assert.equal(headers["Content-Type"], "application/json");
});

test("sendAgentMessage：没有 token 时**不带** Authorization 头（缺身份也要能问车）", async () => {
  const calls = stubFetch(() => ({ ok: true, status: 200, body: { explanation: "好" } }));
  await sendAgentMessage("s1", "预算15万", null);
  const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
  assert.equal(headers.Authorization, undefined);
});

test("writeIdentityToken(null)：清掉本地身份（登出/清数据后不该再带旧身份）", () => {
  const map = stubStorage({ [IDENTITY_KEY]: "tok-old" });
  writeIdentityToken(null);
  assert.equal(map.has(IDENTITY_KEY), false);
  assert.equal(readIdentityToken(), null);
});

// ── 「我记住的偏好」的列表与撤回（2026-10-21）───────────────────────────────

test("listPreferences：带 token 时带 Authorization 头", async () => {
  const calls = stubFetch(() => ({ ok: true, status: 200, body: [{ id: 1, key: "budget" }] }));
  const items = await listPreferences("tok-abc");
  const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
  assert.equal(headers.Authorization, "Bearer tok-abc");
  assert.equal(calls[0].url, "/api/v1/agent/preferences");
  assert.equal(items.length, 1);
});

test("listPreferences：没有 token 时不带 Authorization 头（缺身份也要能读）", async () => {
  const calls = stubFetch(() => ({ ok: true, status: 200, body: [] }));
  const items = await listPreferences(null);
  const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
  assert.equal(headers.Authorization, undefined);
  assert.deepEqual(items, []);
});

test("listPreferences：读失败**抛错**而不是返回空列表", async () => {
  // 空列表会被 UI 读成「你什么也没被记住」——那是谎。读不到必须与「没有」区分开。
  stubFetch(() => ({ ok: false, status: 500, body: { detail: "boom" } }));
  await assert.rejects(() => listPreferences("tok-abc"), /失败/);
});

test("listPreferences：响应不是数组时返回空数组（不把异常形状当列表用）", async () => {
  stubFetch(() => ({ ok: true, status: 200, body: { detail: "unexpected" } }));
  assert.deepEqual(await listPreferences("tok-abc"), []);
});

test("deletePreference：发 DELETE 到那条记忆，且带 token", async () => {
  const calls = stubFetch(() => ({ ok: true, status: 204, body: null }));
  await deletePreference(42, "tok-abc");
  const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
  assert.equal(calls[0].init?.method, "DELETE");
  assert.equal(calls[0].url, "/api/v1/agent/preferences/42");
  assert.equal(headers.Authorization, "Bearer tok-abc");
});

test("deletePreference：撤回失败要抛错（不能静默当作撤回成功）", async () => {
  stubFetch(() => ({ ok: false, status: 404, body: { detail: "not found" } }));
  await assert.rejects(() => deletePreference(42, "tok-abc"), /失败/);
});
