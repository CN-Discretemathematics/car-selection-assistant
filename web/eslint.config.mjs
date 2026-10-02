// ESLint 扁平配置（2026-10-02 建立）
//
// 建立背景：仓库此前**没有任何** ESLint 配置，但代码里已有 6 处抑制注释——
// 4 处 `@next/next/no-img-element`、2 处 `react-hooks/exhaustive-deps`。
// 后者尤其值得注意：它们是那两处依赖数组**唯一的护栏**，却没有门禁在跑，
// 下次改依赖数组不会有人拦。
//
// ── 启用状态：已启用（2026-10-03 首次实跑）──────────────────────────────
// 2026-10-02 建立本文件时**刻意不动 package.json 的 dependencies**：加了依赖却
// 不更新 pnpm-lock.yaml，CI 的 `pnpm install --frozen-lockfile` 会直接失败。
// 于是配置提交了、依赖没装、CI 也没有对应步骤——**第四份「声明了但从不执行」的
// 承诺**（前三份：doc_sync_selftest、前端测试、ruff 规则集）。
//
// 2026-10-03 已用一次真实安装补齐（package.json 与 lockfile 同步更新，故
// --frozen-lockfile 不受影响）：
//     pnpm add -D eslint@^9 eslint-config-next@^15.5.27 @eslint/eslintrc
// 实测基线：**0 error / 0 warning**。CI 的 web job 已接入且**阻塞**
// （`--report-unused-disable-directives --max-warnings 0`）。
//
// 用 --report-unused-disable-directives 验证过：仓库既有 6 处抑制注释
// （4 处 @next/next/no-img-element + 2 处 react-hooks/exhaustive-deps）
// **全部仍在生效**，没有一条是失效的僵尸注释——此前没人能回答这个问题，
// 因为没人跑过它。
//
// 规则口径见 docs/engineering-standards.md §4.2：指标是信号不是目标，
// 新代码从严、存量递减冻结，故此处不对存量开全局 error。

import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({
  baseDirectory: dirname(fileURLToPath(import.meta.url)),
});

const config = [
  {
    ignores: [".next/**", "node_modules/**", "out/**", "next-env.d.ts"],
  },
  // next/core-web-vitals 自带 @next/next/* 与 react-hooks 插件，
  // 正是仓库既有 6 处抑制注释所针对的规则集。
  ...compat.extends("next/core-web-vitals"),
  {
    rules: {
      // 存量基线：2026-10-02 全仓 tsc --noEmit 干净、前端测试 11/11 通过，
      // 故此处只**新增**纪律，不做一次性清理。
      "no-console": ["warn", { allow: ["warn", "error"] }],
      eqeqeq: ["error", "smart"],
      "prefer-const": "error",
    },
  },
  // 测试文件放宽：断言里大量使用 any 与非空断言是刻意的
  {
    files: ["**/*.test.mts", "**/*.test.ts"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
];

// 2026-10-03 首次实跑：直接 export 匿名数组会触发
// import/no-anonymous-default-export（本文件唯一的告警）。改为具名导出。
export default config;
