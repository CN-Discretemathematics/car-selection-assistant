// 「记住的偏好」的人话渲染（2026-10-21）。
//
// 为什么要测：这套映射把后端存的字符串（JSON 数组 / 十进制 / 预算对象）
// 变成用户能看懂的一句话——**说错就是骗人**（用户以为自己记的是 15 万，
// 界面上写 150000 或写错单位）。
//
// 零依赖：Node 22 内置 test runner + 类型剥离（写法对齐 lib/agent.test.mts）。

import test from "node:test";
import assert from "node:assert/strict";

import { describePreference } from "./preferences.ts";
import type { PreferenceMemory } from "./api.ts";

function mem(key: string, value: string): PreferenceMemory {
  return {
    id: 1,
    category: "constraint",
    key,
    value,
    source_type: "explicit",
    confidence: 1,
    confirmed: true,
    created_at: "2026-10-21T00:00:00Z",
    expires_at: null,
  };
}

test("预算：区间", () => {
  assert.equal(
    describePreference(mem("budget", '{"min": 150000, "max": 250000}')),
    "预算：15 万元 ~ 25 万元",
  );
});

test("预算：只有上限 ⇒ 说成「不超过」，不编下限", () => {
  assert.equal(
    describePreference(mem("budget", '{"max": 250000}')),
    "预算：不超过 25 万元",
  );
});

test("预算：只有下限 ⇒ 说成「起」", () => {
  assert.equal(describePreference(mem("budget", '{"min": 150000}')), "预算：15 万元 起");
});

test("人数：带「人」", () => {
  assert.equal(describePreference(mem("passengers", "5")), "用车人数：5 人");
});

test("用途：数组 ⇒ 顿号分隔", () => {
  assert.equal(describePreference(mem("usage", '["家用", "通勤"]')), "用途：家用、通勤");
});

test("车型类别：枚举 ⇒ 中文", () => {
  assert.equal(describePreference(mem("body_type", '["suv", "sedan"]')), "车型类别：SUV、轿车");
});

test("能源偏好：枚举 ⇒ 中文", () => {
  assert.equal(describePreference(mem("energy_preference", '["BEV"]')), "能源偏好：纯电");
});

test("能源偏好：大类别名也要说人话", () => {
  assert.equal(
    describePreference(mem("energy_preference", '["new_energy"]')),
    "能源偏好：新能源",
  );
});

test("不想要的：能源与车身混在一张表里也能各自说人话", () => {
  assert.equal(
    describePreference(mem("avoid", '["BEV", "mpv"]')),
    "不想要：纯电、MPV",
  );
});

test("不想要的品牌：品牌名原样（库里就是品牌名）", () => {
  assert.equal(describePreference(mem("brand_exclude", '["丰田"]')), "不想要的品牌：丰田");
});

test("值不是 JSON 时原样显示，不猜", () => {
  // 解析失败就显示原值——**不补 0、不猜单位**（全站「缺失要显式」的口径）
  assert.equal(describePreference(mem("budget", "150000")), "预算：150000");
});

test("未知键：键名兜底，不静默丢弃", () => {
  assert.equal(describePreference(mem("some_new_key", "x")), "some_new_key：x");
});
