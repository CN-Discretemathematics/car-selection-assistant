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

三处都踩过，共同点是**命令在到达远端之前就被 PowerShell 解析过了**：

1. `git commit -m "……"` 里含引号 / 反引号 / 中文标点会被拆成多个参数：
   `error: pathspec 'docker' did not match any file(s) known to git`。
   → **长提交信息写文件，用 `git commit -F <file>`**（本仓库的 `.tools/commit-msg.txt` 即为此用）。
2. `ssh host "… $(cmd) …"`：`$(...)` 会在**本地**展开；且 PowerShell 的 `curl` 是
   `Invoke-WebRequest` 别名，`-s` / `-o` 会被当成它自己的参数报
   `Missing an argument for parameter 'SessionVariable'`。
   → **远端复杂命令写成脚本 `scp` 过去再 `sh`/`python3` 执行**，不要内联。
3. 嵌套引号（如 `python3 -c "… '…' …"`）几乎必坏，bash 侧报
   `syntax error near unexpected token '('` 或 `unexpected EOF while looking for matching quote`。
   → 同上：一律走脚本文件。
4. 不要用 PowerShell 管道改文件（`Get-Content … | Set-Content`）：会破坏 UTF-8（中文变乱码）
   与行尾。需要改文件用编辑工具，或改完用 `scp` 传 LF 文件过去。

判断口诀：**命令里出现引号嵌套、`$()`、括号、反引号时，不要再拼字符串，直接落成脚本文件。**

## 处理密钥时的硬规则（本次因违反付出代价）

- **不要用 `sed`/`grep` 临场掩码**：写 `sed -E 's/^(OSS_ACCESS_KEY_ID=.{8}).*(.{4})$/\1****\2/'` 只掩码了
  你显式写出的那一行，随后的 `grep OSS_ACCESS` 会把 `OSS_ACCESS_KEY_SECRET=…` 整行原样打印——
  本次因此把一个**生产密钥**打进了对话记录，只能按轮换流程作废重来。
- 核对密钥一律用**程序化指纹**：`sha256(值)[:12]` + 长度 + 掩码形状（字母数字→`x`、保留标点），
  既能判断「两边是否一致」，又不会带出明文（可参照 `.tools/env_fingerprint.py`）。
- 确实需要打印时，按键名逐行处理并只输出 `前 8 + **** + 后 4`；任何"顺手 grep 一下"的念头都要掐掉。
- 密钥一旦出现在对话、日志、截图中，就**按已泄露处理**：立即轮换（先建新 → 切换 → 验证 → 废旧），
  并在轮换记录里注明原因与时间。
