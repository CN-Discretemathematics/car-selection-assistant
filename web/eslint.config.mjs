// ESLint 扁平配置（2026-10-02 建立）
//
// 建立背景：仓库此前**没有任何** ESLint 配置，但代码里已有 6 处抑制注释——
// 4 处 `@next/next/no-img-element`、2 处 `react-hooks/exhaustive-deps`。
// 后者尤其值得注意：它们是那两处依赖数组**唯一的护栏**，却没有门禁在跑，
// 下次改依赖数组不会有人拦。
//
// ── 启用步骤（尚未执行）────────────────────────────────────────────
// 本文件**只提交配置，刻意不动 package.json 的 dependencies**：
// 加了依赖却不更新 pnpm-lock.yaml，CI 的 `pnpm install --frozen-lockfile`
// 会直接失败。因此启用需要一次真实安装（会同时更新 lockfile）：
//
//     cd web
//     pnpm add -D eslint@^9 eslint-config-next@^15.5.0 @eslint/eslintrc
//     pnpm run lint
//
// 在此之前 `pnpm run lint` 会报 "eslint not found"——**这就是待安装的信号**，
// 不是配置错误。CI 的 lint job 当前也**不**跑 eslint，只跑零依赖的
// tools/quality_metrics.py 看板（见 .github/workflows/ci.yml）。
//
// 规则口径见 docs/engineering-standards.md §4.2：指标是信号不是目标，
// 新代码从严、存量递减冻结，故此处不对存量开全局 error。

import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({
  baseDirectory: dirname(fileURLToPath(import.meta.url)),
});

export default [
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
