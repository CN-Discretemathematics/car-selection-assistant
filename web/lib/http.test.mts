import test from "node:test";
import assert from "node:assert/strict";

import { extractErrorDetail } from "./http.ts";

/**
 * `detail` 的类型收窄守卫（2026-10-02 抽取 lib/http.ts 时补上）。
 *
 * 背景：此前 `body?.detail ?? \`请求失败（${status}）\`` 这个写法散落 4 处
 * （auth.ts ×2、rag.ts ×1、compare/page.tsx ×1），而 `body` 来自
 * `res.json().catch(() => null)`，类型是 `any`。
 *
 * FastAPI 的 `RequestValidationError`（422）返回的 `detail` 是**对象数组**，
 * 不是字符串。后果分两种：
 *   - `new Error(detailArray)`：message 变成 "[object Object]"，用户看到无意义文案；
 *   - `setError(detailArray)`：把数组当 React child 渲染，直接抛
 *     "Objects are not valid as a React child"，**整页白屏**。
 */
test("detail 是字符串：原样透出（与旧实现逐字一致）", () => {
  assert.equal(extractErrorDetail({ detail: "款型不存在" }, "兜底"), "款型不存在");
});

test("detail 是数组（FastAPI 422）：回退到状态码文案，不返回对象", () => {
  const body = {
    detail: [
      { loc: ["body", "variant_ids"], msg: "field required", type: "missing" },
    ],
  };
  const out = extractErrorDetail(body, "请求失败（422）");
  assert.equal(out, "请求失败（422）");
  assert.equal(typeof out, "string", "必须是字符串：非字符串会被当 React child 渲染");
});

test("detail 是对象：同样回退", () => {
  assert.equal(extractErrorDetail({ detail: { code: 1 } }, "请求失败（500）"), "请求失败（500）");
});

test("无 detail / detail 为空串：回退", () => {
  assert.equal(extractErrorDetail({}, "兜底"), "兜底");
  assert.equal(extractErrorDetail({ detail: "" }, "兜底"), "兜底");
  assert.equal(extractErrorDetail(null, "兜底"), "兜底");
});

test("body 不是对象（后端返回 HTML 错误页时 json() 失败 → null）", () => {
  assert.equal(extractErrorDetail(null, "请求失败（502）"), "请求失败（502）");
  assert.equal(extractErrorDetail("plain text", "兜底"), "兜底");
});

test("不会把数组里的内容字符串化后返回", () => {
  // 旧实现的 `body?.detail` 在此处会返回数组本身；这里必须保证返回的是字符串。
  const out = extractErrorDetail({ detail: [{ msg: "x" }] }, "fallback");
  assert.ok(!Array.isArray(out));
});
