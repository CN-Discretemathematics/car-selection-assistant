// 前端纯逻辑回归测试（零依赖：Node 22 内置 test runner + 类型剥离）。
//
// 为什么是 .mts：node 的 ESM 解析要求导入路径带扩展名（./api.ts），而 TS 默认
// 禁止 .ts 扩展名导入（TS5097）。tsconfig 的 include 只含 **/*.ts，不含 .mts，
// 故本文件不参与 tsc --noEmit 检查，两种约束得以共存。
//
// 运行：pnpm test（= node --experimental-strip-types --test "lib/**/*.test.mts"）

import test from "node:test";
import assert from "node:assert/strict";

import { formatCount, formatPrice, formatPriceRange, wanToYuan, yuanToWan } from "./api.ts";
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

test("yuanToWan：整万元不得丢数字（曾把 10 万显示成 1 万）", () => {
  // 本 bug 的核心：旧实现 String(v / 10000).replace(/\.?0+$/, "") 把 "10" 的
  // 末位 0 当作小数尾零裁掉，得到 "1"，回写后价格窗口缩水十倍。
  assert.equal(yuanToWan("100000"), "10");
  assert.equal(yuanToWan("200000"), "20");
  assert.equal(yuanToWan("300000"), "30");
  assert.equal(yuanToWan("1000000"), "100");
});

test("yuanToWan：保留有意义的小数位", () => {
  assert.equal(yuanToWan("105000"), "10.5");
  assert.equal(yuanToWan("200500"), "20.05");
});

test("yuanToWan：空值与非数字返回空串（不下发该筛选参数）", () => {
  assert.equal(yuanToWan(null), "");
  assert.equal(yuanToWan(undefined), "");
  assert.equal(yuanToWan(""), "");
  assert.equal(yuanToWan("abc"), "");
});

test("wanToYuan：万元写回元", () => {
  assert.equal(wanToYuan("10"), 100000);
  assert.equal(wanToYuan("10.5"), 105000);
});

test("wanToYuan 与 yuanToWan 往返一致（显示后再回写不得丢量级）", () => {
  // 这是本缺陷最直接的回归防线：URL 里的 100000 显示成 "10"，用户改任意其它
  // 筛选项后被回写，必须仍是 100000 而不是 10000。
  for (const yuan of ["100000", "200000", "1000000", "105000", "200500"]) {
    assert.equal(wanToYuan(yuanToWan(yuan)), Number(yuan), `往返失败：${yuan}`);
  }
});

test("wanToYuan：空值、非数字与负值返回 null", () => {
  assert.equal(wanToYuan(""), null);
  assert.equal(wanToYuan("abc"), null);
  assert.equal(wanToYuan("-5"), null);
});
