// 前端纯逻辑回归测试（零依赖：Node 22 内置 test runner + 类型剥离）。
//
// 为什么是 .mts：node 的 ESM 解析要求导入路径带扩展名（./api.ts），而 TS 默认
// 禁止 .ts 扩展名导入（TS5097）。tsconfig 的 include 只含 **/*.ts，不含 .mts，
// 故本文件不参与 tsc --noEmit 检查，两种约束得以共存。
//
// 运行：pnpm test（= node --experimental-strip-types --test "lib/**/*.test.mts"）

import test from "node:test";
import assert from "node:assert/strict";

import { formatCount, formatPrice, formatPriceRange } from "./api.ts";
import type { PriceRange } from "./api.ts";

const range = (min: number | null, max: number | null): PriceRange => ({
  currency: "CNY",
  type: "guide",
  min,
  max,
});

test("formatPrice：元转万元，保留有意义的小数位", () => {
  assert.equal(formatPrice(105000), "10.5 万元");
  assert.equal(formatPrice(200500), "20.05 万元");
});

test("formatPrice：整万元不得丢数字（10 万不得显示为 1 万）", () => {
  // 回归防线。本函数先 toFixed(2) 再裁尾零，故整十万/整百万安全：
  //   100000 -> "10.00" -> "10"（裁掉 ".00"）
  // 相对的，筛选表单的换算曾漏掉 toFixed(2)，直接 String(v/10000) 裁尾零，
  //   100000 -> "10" -> "1"（裁掉末位 "0"），价格窗口被静默收窄到 1 万。
  // 见 HomeFilters / BrowseFilters 的修复提交。
  assert.equal(formatPrice(100000), "10 万元");
  assert.equal(formatPrice(200000), "20 万元");
  assert.equal(formatPrice(1000000), "100 万元");
});

test("formatPrice：空值返回占位文案", () => {
  assert.equal(formatPrice(null), "暂无");
});

test("formatPriceRange：区间、单点、开区间与全空各自成文", () => {
  assert.equal(formatPriceRange(range(150000, 200000)), "15 万元 ~ 20 万元");
  assert.equal(formatPriceRange(range(150000, 150000)), "15 万元");
  assert.equal(formatPriceRange(range(null, 200000)), "最高 20 万元");
  assert.equal(formatPriceRange(range(150000, null)), "15 万元 起");
  assert.equal(formatPriceRange(range(null, null)), "官方指导价：暂无");
});

test("formatCount：zh-CN 千分位", () => {
  assert.equal(formatCount(12345), "12,345");
});
