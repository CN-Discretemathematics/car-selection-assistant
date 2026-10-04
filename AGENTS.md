# car-selection-assistant 工程约定（每次会话必读）

车型选择助手：`backend/`（FastAPI + SQLite/PG）+ `web/`（Next.js）+ `reviewer/`（文案/密钥门禁）
+ `skills/`（实测沉淀）+ `docs/`。分支一律从最新 `origin/main` 拉，`main` 只走 PR。

## 并行会话纪律（2026-09-16 串台事故后的硬规则，违反即返工）

两个 agent/会话并行开发不同功能时，上下文与工作区必须隔离。以下规则对应一次实测事故：
共用提交信息文件导致 compare 功能提交顶着前端美化轮的信息被推上远端。

1. **提交信息独立文件**：每次提交把信息写到 `.tools/commit-msg-<branch>-<hhmm>.txt`
   再 `git commit -F`，用完删。**严禁共用固定文件 `.tools/commit-msg.txt`**——
   开工时先看 `.tools/` 里有没有别的会话刚写的 commit-msg 文件：
   `Get-ChildItem .tools/commit-msg-* | Sort-Object LastWriteTime`（半小时内有更新 = 有并行会话）。
2. **精确暂存**：只 `git add <明确文件清单>`，严禁 `add -A` / `add .`。开工时 `git status`
   发现有**不属于自己**的未暂存改动，不要动、不要提交；若它挡路（同文件冲突），
   停下来向编排者（人类）报告，不要自行 stash/checkout 别人的工作现场。
3. **push 前逐提交核对**：`git log --oneline origin/<branch>..HEAD` +
   `git show --stat` 每个提交——信息说的功能与改动的文件必须是同一件事。
4. **push 门禁自动拦截**：`.githooks/pre-push` 已接线（`git config core.hooksPath .githooks`），
   每次 push 自动运行 `tools/pre-push-guard.py`，核对「信息 scope ↔ 实际改动文件」，
   不符即拦截。确属跨切面的合法提交才允许 `PUSH_GUARD_ALLOW=1` / `--no-verify` 显式放行。
   clone/新 worktree 后必须执行一次：`git config core.hooksPath .githooks`。
   门禁自测（应 FAIL 才对）：`python tools/pre-push-guard.py 10e42c8..de7985a`。
5. **一分支一功能**：并行功能各自开分支；工作区真冲突时用 worktree 物理隔离：
   `git worktree add ../carSelection-<feat> -b <branch>`。共享文件并行期间尽量不动，
   必须动就在提交信息里注明与原因：`pnpm-lock.yaml`、`web/lib/api.ts`、后端 router 注册
   （`backend/app/main.py`）、`web/app/layout.tsx`、`AGENTS.md`、`docs/`。
6. **scope 登记**：新功能分支的 scope token 要登记进 `tools/pre-push-guard.py` 的
   `SCOPE_PATHS`，否则门禁只 WARN 不拦截。

## 推送前必须确认 base 是最新的 `origin/main`（2026-10-05 用户定规）

**规则**：任何分支在 `git push` 之前，必须确认它的基线是最新的 `origin/main`。
落后就先 `git rebase origin/main`（或 `git merge origin/main`）再推。

**为什么**（两次实测踩坑，不是假想）：
- #59 与 #60 各自改了 README 的用例计数，#60 未 rebase 直接推 → GitHub 报
  `mergeable_state=dirty`，评审打开就是冲突，必须回头 rebase + 重算计数。
- 规则本身比「推之前看一眼」更强：并行会话下**冲突是必然的，不是意外**。

**怎么确认**（推送前跑这一条）：

```powershell
git fetch origin
git log --oneline origin/main..HEAD   # 本分支领先 main 什么
git log --oneline HEAD..origin/main   # main 领先本分支什么 ← 这个非空就是落后了
```

第二条**非空即必须先 rebase**。rebase 后 README 等共享文件可能冲突——
**取 main 侧**，再用 `python skills/doc_sync_check.py --fix` 按新 base 重算计数，
**不要沿用 rebase 前的数字**。

**叠放 PR 的坑**（同源问题，务必一起看）：
PR 的 base 可以是 `main`，**不要**为了「避开同文件冲突」把 base 设成另一个分支——
那样合并只会推进那个中间分支，`main` 拿不到内容，而 GitHub 上显示 `merged`，
极易误判为「已进主干」（#56/#57 就这样丢过一次内容）。

## 验证基线（提交前实跑，不沿用旧数字）

- backend：`backend\.venv\Scripts\python.exe -m pytest -q`（在 `backend/` 下）
- web：`pnpm --dir web exec tsc --noEmit`；改 UI 后按需 `next build`（先停 `next dev`，
  两者共用 `.next` 会互相破坏）
- **eslint（2026-10-03 起已装且阻塞）**：`cd web` 后 `pnpm exec eslint . --max-warnings 0`。
  基线 0。注意本机 pnpm 与 CI 的 pnpm major 不同（见 docs/refactoring-roadmap.md 的
  「pnpm 版本三处不一致」），`web/pnpm-workspace.yaml` 已被 gitignore——**不要提交它**，
  提交会让 CI 的 `pnpm install --frozen-lockfile` 直接失败。
- 文案/密钥/文档门禁：`reviewer/scan_ui_copy.py`、`reviewer/scan_secrets.py`、
  `skills/doc_sync_check.py`（计数/悬空引用/配置表/编码；退出码必须 0，CI gates job 已接线。
  2026-09-16 实例：README 用例数 313 未随实际 347 更新，就是它抓出来的。
  **判定真值 = git 索引**（本地跑 == CI 跑，gitignore 的本地专属文件只 WARN）；
  `--fix` 自愈确定性计数；改过判定逻辑后跑 `python skills/doc_sync_selftest.py`）
- 收尾（commit 前，语义层）：本分支的代码改动会影响文档事实时，先跑**文档评审 subagent**
  （输入/产出/完整 prompt 见 `skills/doc-sync.md`），它直接改文档并出报告，报告并入提交说明；
  机械计数交给 `doc_sync_check.py --fix`；两道都过再 commit
- 端到端判据先定义再动手（真后端 + 真 DOM 断言），参考 `skills/` 同类脚本。
- **ruff（2026-10-03 起可跑）**：`backend\.venv\Scripts\python.exe -m ruff check .`
  在 `backend/` 下。基线 **130 条**，CI `lint` job 只报不拦。**不要无脑 `--fix`**：
  I001 会把 `app.*` 排到 `load_dotenv()` 之前（搞坏环境加载），F401 会删掉
  `__init__.py` 的 re-export（外部 import 崩）。改动后若计数上升需在提交信息里说明。
  它的 `target-version = "py311"` 是本仓 3.11 兼容承诺的**唯一自动化执行点**——
  3.12-only 语法（如 f-string 内反斜杠）只有它能抓，CI 与本地 pytest 都不会红。

## Instincts（0.9 级教训，每次会话生效；完整库见 docs-local/instincts.md）

1. **测试环境必须能复现生产路径**：种子/夹具缺生产实体时，断言的是与生产不同的
   代码路径，生产 bug 被测试掩蔽（金标 row36 教训）——补种子时要问「生产库里有没有它」。
2. **改 async 任务/协程签名必须全仓搜调用点**：旁路任务（create_task）异常不传播，
   调用点不匹配 = 静默死亡、日志零痕迹（_shadow_route 教训）——E2E 测试是唯一信号。
3. **测试必须对环境免疫**：conftest 显式固定/清除环境变量，不要求本机 .env 保持
   特定内容（retrieval/config.py 的 load_dotenv 会把 .env 灌进 os.environ）。

## 其他

- Windows/PowerShell 命令卫生、分支同步、冲突解决、密钥处理：见 `skills/git-branch-sync.md`
  （含本次串台事故完整复盘）。
- 提交信息格式：`type(scope): 摘要`，scope 与改动路径必须一致（门禁 R1–R4 强制）。
