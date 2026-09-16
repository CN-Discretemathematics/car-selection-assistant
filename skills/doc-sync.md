# 文档与代码自动对齐（doc-sync）

来源：2026-09-15 文档对齐轮（一次性修复 20+ 处漂移后沉淀，含 UTF-16 编码事故）。
适用阶段：全阶段（交付流程类 skill，属开发侧，不进用户侧 Agent 注入集）。
可执行检查：`skills/doc_sync_check.py`（只读，FAIL 时退出码 1）；**已接入 CI**
（`.github/workflows/ci.yml` 的 gates job，PR 阶段即拦截）。

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

## 触发时机（自动执行，不需要人提）

- **CI（已接线，最可靠）**：`.github/workflows/ci.yml` 的 gates job 在每次 PR 与 main push 上
  运行本检查——文档漂移会在 PR 阶段被拦下，不再依赖「Agent 记得跑」；
- **会话开始**：`git log --oneline -5` / `git status` 显示有新提交或代码改动（`backend/`、
  `web/`、`deploy/` 下文件），且尚未跑过本轮检查；
- **代码改动任务完成时**：本次会话改过任何代码文件，收尾前必跑；
- **commit / PR 前**：与 `reviewer/scan_secrets.py` 一起构成提交前门禁。

## 工作流

```powershell
# ① 检测（判定依据 = 脚本退出码）
cd backend; $env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe ..\skills\doc_sync_check.py

# ② 对 FAIL 逐条读代码对齐（代码是事实源，文档向代码靠拢）
# ③ 复跑至「结论：一致」；把本轮文档改动写进 PR/提交说明
```

脚本覆盖 8 类检查：全量用例数（pytest 实测）、迁移数、LLM 工具 schema 数、README 项目结构
vs 实际模块、文档反引号路径悬空、文档环境变量在代码中可读、.md 编码卫生（UTF-8 无 BOM）、
已知过期表述。

## 对齐规则（改什么、不改什么）

| 情形 | 动作 |
| --- | --- |
| 计数类（用例数/迁移数/工具数/模块清单） | **直接改文档**为实测值 |
| 悬空引用 | 查证代码后改为现役路径；若能力被删除，改为描述现状 |
| 配置表缺项/口径冲突（如 RRF 权重） | 以 `config.py` / `.env.example` 为准改文档 |
| 编码损坏 | Python 脚本转 UTF-8（无 BOM、LF），不用 PowerShell 改文本 |
| 历史快照（`CHANGES.md`、`backend/eval/` 报告） | **不改历史数字**，只在文件头加注「历史时点记录，后续见 git」 |
| 评测指标、产品口径等 Agent 无法从代码推出的数字 | **不猜**——查 eval 报告/git 记录，找不到就在回复里列出待人工确认 |

## 回归要点

- 新增公开文档/新写配置表后，把它纳入 `doc_sync_check.py` 的扫描范围（`DOC_DIRS` /
  `ENV_DOC_NAMES`），并确认脚本仍「结论：一致」；
- 若某条检查误报，优先收紧判定（如 `ALLOW_WORDS` 行内豁免、`RUNTIME_PREFIXES`），
  不允许为过检而删检查项；
- 本 skill 只保证「文档陈述与代码一致」，不保证文档内容*该不该*写——内容取舍仍由人审。
