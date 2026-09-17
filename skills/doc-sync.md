# 文档与代码自动对齐（doc-sync）

来源：2026-09-15 文档对齐轮（一次性修复 20+ 处漂移后沉淀，含 UTF-16 编码事故）。
适用阶段：全阶段（交付流程类 skill，属开发侧，不进用户侧 Agent 注入集）。
可执行检查：`skills/doc_sync_check.py`（默认只读，FAIL 时退出码 1；`--fix` 自愈确定性计数）；
**已接入 CI**（`.github/workflows/ci.yml` 的 gates job，PR 阶段即拦截）。
判定真值 = **git 索引**（不是本地工作区），故「本地跑 == CI 跑」；检查器自身的回归自测：
`python skills/doc_sync_selftest.py`（在 `.tmp/` 建 tracked-only 检出真跑）。
CI 侧另有 `autofix` job：gates 失败时自动 `--fix` 并把计数补丁推回 PR 分支（仅同仓库 PR）。

## 为什么需要（真实事故）

文档是「代码的旧快照」，会以四种方式静默烂掉：

1. **计数漂移**：README 写 289 用例、DELIVERY 写 189/179——实际已 298；「7 个迁移」实际 8 个；
   2026-09-16 复发一次：README 写 313 用例、实际已 347（当天新增 5 个分支的功能都没同步），
   说明「靠 Agent 记得跑」不够可靠——所以本检查已接进 CI，见下。
2. **悬空引用**：DEPLOYMENT.md 引用 `deploy/systemd/*.service`、`deploy/deploy.sh`、
   `tools/replace_sample_data.py`、`register_sales_task.ps1`——文件早已删除；RAG.md 开头引用
   断成空反引号；2026-09-16 又出现**反例**：根级 `tools/pre-push-guard.py` 真实存在却被误报
   悬空（检查器的 `ROOT_PREFIXES` 不认识新增的根级 `tools/`）——说明检查器自身也要随结构演进维护。
3. **口径冲突**：RAG_TECH_SELECTION.md 写「路权重按 query_type 分路」，实现早已统一 0.6/0.4；
   RAG.md 环境变量表漏掉 `RETRIEVAL_TOKENIZER` / `RERANK_API_PATH` / `RETRIEVAL_RRF_WEIGHT_*`；
4. **编码损坏**：CHANGES.md、RAG_TECH_SELECTION.md 被 PowerShell `>` 重定向写成 UTF-16，
   GitHub 显示乱码、读取工具判为二进制（判别信号见全局 AGENTS.md §1）。
5. **门禁判据与 CI 不一致（2026-09-16 PR #29 事故）**：检查器用 `Path.exists()` 判悬空引用，
   而本地工作区多了 gitignore 的「本地专属」文件（`PROJECT_PLAN.md`、`reviewer/REVIEWER_AGENT.md`、
   `resume/`、`DEPLOYMENT.md`…）：**本地 exit 0「结论：一致」，CI 同一提交 FAIL 3 处**
   （`16 份文档纳入检查` vs 本地 55 份）——门禁在 push 前无法自证，要等 PR 页面才发现。
   修法：判定真值改为 git 索引（`git ls-files` / `git check-ignore`），并给检查器配回归自测。

## 触发时机（自动执行，不需要人提）

- **CI（已接线，最可靠）**：`.github/workflows/ci.yml` 的 gates job 在每次 PR 与 main push 上
  运行本检查——文档漂移会在 PR 阶段被拦下，不再依赖「Agent 记得跑」；
- **CI 自动修复（2026-09-16 接入）**：gates 失败时同 workflow 的 `autofix` job 跑 `--fix`，
  有补丁就作为新提交推回 PR 分支（仅同仓库 PR；仓库需开 Actions 写权限），下一轮 CI 复跑确认。
  它只碰确定性计数——判读类 FAIL 仍按下面的工作流人工对齐（不给「改文档骗过门禁」留后门）；
  推回的提交正文列出每条 FIX，PR 时间线可直接看到自动改了什么；
- **会话开始**：`git log --oneline -5` / `git status` 显示有新提交或代码改动（`backend/`、
  `web/`、`deploy/` 下文件），且尚未跑过本轮检查；
- **代码改动任务完成时**：本次会话改过任何代码文件，收尾前必跑；
- **commit / PR 前**：先跑文档评审 subagent（见下节，语义层），再与 `reviewer/scan_secrets.py`、
  `doc_sync_check.py` 一起构成提交前门禁（机械层）。

## 工作流

```powershell
# ① 检测（判定依据 = 脚本退出码）
cd backend; $env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe ..\skills\doc_sync_check.py

# ② 计数类 FAIL 可先自愈：只改写能算出来的数字（打印每条改动），判读类一律不动
python ..\skills\doc_sync_check.py --fix

# ③ 对剩余 FAIL 逐条读代码对齐（代码是事实源，文档向代码靠拢）
# ④ 复跑至「结论：一致」；把本轮文档改动写进 PR/提交说明
# ⑤ 改过判定逻辑（token 解析 / 悬空判定 / --fix 目标）后必须跑检查器自测：
python ..\skills\doc_sync_selftest.py
```

脚本覆盖 8 类检查：全量用例数（pytest 实测）、迁移数、LLM 工具 schema 数、README 项目结构
vs 实际模块、文档反引号路径悬空、文档环境变量在代码中可读、.md 编码卫生（UTF-8 无 BOM）、
已知过期表述。其中用例数/迁移数/工具 schema 数可由 `--fix` 自愈。CI 的 gates 与 autofix
两个 job 都会装 `backend/requirements.txt` 并设 `RETRIEVAL_BACKEND=inmemory`，所以用例数在 CI
也真核对（没有 `backend/.venv` 时检查器自动退回当前解释器）；依赖没装齐则该项 WARN 跳过、
其余检查照常，结论仍然有效。

## 文档评审 subagent（推送前语义层，2026-09-16 接入）

机械检查核验「数字/路径/编码」这类可判定事实；**语义漂移**（新能力没写进 README、描述与
代码行为不符、文档之间口径矛盾）只有读懂 diff 才能判断——由文档评审 subagent 在会话收尾
（commit/push 前）执行并**直接改文档**，人退出「文档同步」专项循环，只在 PR review 终审。

**接线与边界**：

- 挂在会话收尾流程（AGENTS.md 验证基线），**不进 git hook**——hook（pre-push-guard）保持
  纯确定性，LLM 的慢/贵/不确定不能卡 push；
- CI 保持机械门禁不动：subagent 的产出由 gates 客观验收（判定真值 = git 索引，本地跑 == CI 跑，
  subagent 可以信任自己的复跑结果），计数漂移另有 autofix 兜底；
- prompt 入库在本文件（跨会话/跨机器可复现），不像 `reviewer/REVIEWER_AGENT.md` 那样本地专属。

**输入**：`git diff origin/main...HEAD`（代码是唯一事实源）＋ `doc_sync_check.py` 当前输出 ＋
本分支涉及的全部 tracked 文档。
**产出**：直接的文档编辑 ＋ 报告（每处修改一行：`文件:位置 ｜ 改了什么 ｜ 依据的代码事实`）＋
BLOCKED / 待人工确认清单；零发现也要明说「未发现语义漂移」。
**验收**：复跑 `doc_sync_check.py` 至「结论：一致」；报告并入提交说明 / PR 描述。

Prompt 模板（整段交给 subagent）：

```text
你是文档一致性评审员，任务是让本分支的公开文档与代码实际行为一致。

先收集事实：
1. git diff origin/main...HEAD --stat 与逐文件 diff——代码是唯一事实源；
2. python skills/doc_sync_check.py 当前输出——机械类不一致不用你算，交给它（计数类可 --fix）；
3. 通读本分支涉及的全部 tracked .md（README、AGENTS.md、docs/、skills/；历史快照 CHANGES.md
   与 gitignore 的本地专属文件除外）。

按序审查四个维度：
A 行为漂移：diff 改了行为（接口/默认值/口径/约束/命令用法），文档是否如实跟改？
B 新能力缺描述：新增的用户可见能力，README/对应文档有没有如实的段落？没有就补在正确位置。
C 删除残留：删掉的能力/文件是否还活在文档正文里？
D 文档互斥：同一事实在两份文档里说法不一？

纪律：
- 只改 .md，不改任何代码；认为代码错了就在报告里标 BLOCKED 并给出证据，不要动手改代码；
- 每处修改必须能指认对应的代码事实（文件+行为）；推不出来的不改，列进「待人工确认」；
- 计数类数字（用例数/迁移数/工具 schema 数）一律交给 doc_sync_check.py --fix，不要手改；
- 编辑一律用编辑工具或 Python（encoding="utf-8"、newline="\n"），严禁 PowerShell 重定向
  改文本（会把 UTF-8 写坏成 UTF-16/乱码，仓库红线）；
- 完成后复跑 doc_sync_check.py：必须「结论：一致」；仍 FAIL 的判读类如实报告，不要硬编；
- 输出报告：修改清单 + BLOCKED + 待人工确认；零发现也要明说「未发现语义漂移」。
```

## 对齐规则（改什么、不改什么）

| 情形 | 动作 |
| --- | --- |
| 计数类（用例数/迁移数/工具数/模块清单） | **直接改文档**为实测值；前三项可 `--fix` 自动完成，模块清单需人改 README 结构块 |
| 悬空引用 | 查证代码后改为现役路径；若能力被删除，改为描述现状 |
| 引用 gitignore 的本地专属文件（如 `PROJECT_PLAN.md`） | **不算悬空**：`--fix`/检查器只 WARN（`本地专属引用…CI 检出不含`），因为 CI 检出不见它是预期 |
| 配置表缺项/口径冲突（如 RRF 权重） | 以 `config.py` / `.env.example` 为准改文档 |
| 编码损坏 | Python 脚本转 UTF-8（无 BOM、LF），不用 PowerShell 改文本 |
| 历史快照（`CHANGES.md`、`backend/eval/` 报告） | **不改历史数字**，只在文件头加注「历史时点记录，后续见 git」 |
| 评测指标、产品口径等 Agent 无法从代码推出的数字 | **不猜**——查 eval 报告/git 记录，找不到就在回复里列出待人工确认 |

## 回归要点

- 新增公开文档/新写配置表后，把它纳入 `doc_sync_check.py` 的扫描范围（`DOC_DIRS` /
  `ENV_DOC_NAMES`），并确认脚本仍「结论：一致」；
- 若某条检查误报，优先收紧判定（如 `ALLOW_WORDS` 行内豁免、`RUNTIME_PREFIXES`），
  不允许为过检而删检查项；
- **判定真值永远是 git 索引**：`Path.exists()` 只允许出现在 git 不可用的退回分支里——
  本地存在、未入库的文件被当成「存在」，就是 PR #29 那次「本地绿 / CI 红」的根因；
- 改动判定逻辑后必须跑 `python skills/doc_sync_selftest.py` 并且 6/6 通过
  （它覆盖：CI 视角结论一致、本地专属引用只 WARN、`--fix` 自愈、无 venv 时不猜、
  未入库引用 FAIL、真悬空 FAIL）；
- 本 skill 的机械层只保证「文档陈述与代码一致」；「该不该写、写得对不对」由文档评审 subagent
  把关（见「文档评审 subagent」节），人在 PR review 终审。
