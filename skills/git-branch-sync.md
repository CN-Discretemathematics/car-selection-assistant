# Skill：分支同步与交付命令卫生（开工 / 推送 / 提 PR）

- 用途：让功能/运维分支始终基于最新 `main`，避免「Behind N」式分叉与后期集中冲突；
  叠放分支（B 基于 A）被上游 rebase 后，只重放自己的提交；并规避 Windows 侧命令解析
  与凭据处理这两类「一犯就出事」的操作。
- 来源：2026-09 分支拆分与同步实战沉淀（`release/security-ops` 与
  `feature/search-and-agent-entry` 从旧基点 `04631b1` 同步到 `main` `68971d3`），
  以及同期的 PowerShell 引号事故与 OSS AK 泄露事故复盘。
- 适用阶段：全阶段——开工拉分支、推送前、提 PR 前各一次；涉及密钥的任何操作。
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

## PowerShell 引号陷阱（本次实测，代价最高的一类）

> **本条已提升为用户全局记忆 `~/.dsh/AGENTS.md` §1**（每次会话自动加载，不再依赖本 skill 被调用）。
> 2026-09-14 教训：写进项目 skill 之后仍重犯两次（`ssh` 内联引号、PowerShell 改文本把中文 docstring 写坏），
> 所以通用教训放全局，此处只保留本仓库的具体做法。

要点回顾（细节见全局记忆）：

1. 长提交信息写文件 → `git commit -F .tools/commit-msg.txt`（本仓库约定路径）；
2. 远端命令写成脚本 `scp` 过去再 `sh`/`python3` 执行（本仓库 `.tools/*.sh` 即此用途），
   不要内联 `ssh host "… $(…) …"`；PowerShell 的 `curl` 是 `Invoke-WebRequest` 别名，
   需要 `curl.exe` 或 `-UseBasicParsing`；
3. 嵌套引号几乎必坏（`unexpected EOF while looking for matching quote`）；
4. 改文件用编辑工具；批量替换写 Python 脚本（`encoding='utf-8'` + `newline='\n'`，
   每处替换断言"恰好命中一次"），改完用 `git diff` 与编译器复核。

判断口诀：**命令里出现引号嵌套、`$()`、括号、反引号时，不要再拼字符串，直接落成脚本文件。**
**改文件不要经过 PowerShell 的字符串管道。**

## 前端/构建类环境陷阱（同期实测）

- **不要在 `next dev` 运行时执行 `next build`**：两者共用 `.next` 目录会互相破坏，症状是
  dev 端报 `Cannot find module './448.js'` / `vendor-chunks/…` 并整页 500——看着像代码坏了，
  其实停掉 dev、删掉 `.next`、重启 dev 即可恢复。要跑生产构建就先停 dev。
- **CSS 动画残留 `transform` 会创建层叠上下文**：筛选栏 `.glass` 带 `animate-fade-up`，
  动画结束后 `transform` 仍是 `matrix(1,0,0,1,0,0)`（非 none），于是栏内 `z-50` 的下拉浮层
  被下方卡片盖住。修法是给容器显式 `relative z-N`（本仓库用 `z-20`，低于导航 `z-30`、
  悬浮助手 `z-40`），而不是继续抬高浮层自身的 z 值。
- **浮层是否真在最上层用 `document.elementFromPoint(x, y)` 判断**，比肉眼看截图可靠
  （半透明浮层在缩略图里极易误判）。
- 半透明浮层（`bg-white/95` + `backdrop-blur`）会让下层卡片文字透出来，深色文字叠浅色底时
  尤其明显；下拉这类需要精确阅读的浮层用**实心白底**。

## 处理密钥时的硬规则（本次因违反付出代价）

- **不要用 `sed`/`grep` 临场掩码**：写 `sed -E 's/^(OSS_ACCESS_KEY_ID=.{8}).*(.{4})$/\1****\2/'` 只掩码了
  你显式写出的那一行，随后的 `grep OSS_ACCESS` 会把 `OSS_ACCESS_KEY_SECRET=…` 整行原样打印——
  本次因此把一个**生产密钥**打进了对话记录，只能按轮换流程作废重来。
- 核对密钥一律用**程序化指纹**：`sha256(值)[:12]` + 长度 + 掩码形状（字母数字→`x`、保留标点），
  既能判断「两边是否一致」，又不会带出明文（可参照 `.tools/env_fingerprint.py`）。
- 确实需要打印时，按键名逐行处理并只输出 `前 8 + **** + 后 4`；任何"顺手 grep 一下"的念头都要掐掉。
- 密钥一旦出现在对话、日志、截图中，就**按已泄露处理**：立即轮换（先建新 → 切换 → 验证 → 废旧），
  并在轮换记录里注明原因与时间。
