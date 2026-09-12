# Skill：分支同步与叠放分支变基（交付前流程）

- 用途：让功能/运维分支始终基于最新 `main`，避免「Behind N」式分叉与后期集中冲突；
  叠放分支（B 基于 A）被上游 rebase 后，只重放自己的提交。
- 来源：2026-09 分支拆分与同步实战沉淀（`release/security-ops` 与
  `feature/search-and-agent-entry` 从旧基点 `04631b1` 同步到 `main` `68971d3`）。
- 适用阶段：全阶段——开工拉分支、推送前、提 PR 前各一次。
- 最后验证：2026-09（两条分支 rebase 后 behind=0；运维分支 235 用例、功能分支 245 用例，
  `tsc --noEmit` 与 `next build` 全绿，compose 经 YAML 解析验证）。

## 流程

```bash
# 1. 开工：先同步基线，再从最新 main 拉分支（不要从本地旧 main 或旧分支拉）
git fetch origin
git switch -c feature/<name> origin/main

# 2. 开发中：每次推送前判断是否落后（Behind 只在 fetch 成功后可信）
git fetch origin
git rev-list --count HEAD..origin/main     # >0 = 落后，先 rebase 再推
git rebase origin/main
```

## 叠放分支：A 被 rebase 后，B 怎么接

```bash
# 只重放 B 自己的提交
git rebase --onto <A 的新尖端> <B 的旧基点 sha> feature/B

# 反例：直接 git rebase A —— git 会把「已在 A 中被重写掉的旧提交」也重放一遍，
# 表现为第一个冲突出现在别人的提交信息上（本次实测踩过：Rebasing (1/6) 撞上旧 ops 提交）
```

判断旧基点：`git log --oneline <候选 sha> -1` 核对提交信息，或从 `git reflog` / 备份分支取。

## 冲突解决原则

- 逐块读两边的意图再合并，**不整段照抄一侧**；
- 版本号与统计数字（测试用例数、评测指标）一律 rebase 后**实跑取真值**，
  不沿用任何一侧的旧值；
- rebase 完成后必须重跑测试与构建——自动合并（git 说 auto-merging）同样可能弄坏代码；
- 结构性文件（YAML / compose / 配置）用解析器验证，而不是只确认「没有冲突标记」；
- 冲突解决后 `git add` + `git rebase --continue`；想放弃用 `git rebase --abort`。

## 推送与回退

```bash
git branch backup/<branch>-<旧 sha> <branch>     # 动手前留回退点
git push --force-with-lease origin <branch>     # 历史被重写后用它，不用 --force
```

- 已推送分支 rebase 会改写历史：只在**自己的**功能/运维分支上用 `--force-with-lease`；
- `main` 永不 force-push、永不直接推送，一律走 PR。

## 注意

- 网络受限时先带代理 fetch：`git -c http.proxy=<proxy> fetch origin`；fetch 失败时
  本地 `origin/main` 是旧引用，`rev-list --count HEAD..origin/main` 会误报 0（本次
  实际落后 40 却显示 0，是 GitHub 页面提醒才发现）。
- `-c` 是 git 的全局选项，必须写在子命令前：`git -c http.proxy=... push ...`；
  写成 `git push -c ...` 会把 `-c` 当成 push 自己的参数而报错。
- 提交前 `git status` 必须干净；`.env*`、`.deploy/`、`.tools/`、`vendor/`、
  `backend/eval/TODO-internal.md` 等本地文件均在 `.gitignore`，不要误加。
- Windows 上用 PowerShell 时不要用管道改文件（会破坏 UTF-8 与行尾），需要改文件用编辑工具
  或 scp 传 LF 文件。
