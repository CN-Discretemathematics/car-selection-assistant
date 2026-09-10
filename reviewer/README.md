# Reviewer（代码审查 Agent）

本目录是 carSelection 项目的**独立代码审查 Agent** 配套文件，用于在主 Agent 完成
代码任务后执行代码审查，确保代码规范，**最优先保障：任何 API Key / 机密不得进入
会被上传或提交的文件**。

## 目录内容

| 文件 | 用途 |
|---|---|
| `REVIEWER_AGENT.md` | 审查员身份定义、密钥红线、触发时机、审查流程、规范清单、报告模板。主 Agent 完成任务后，将本文件全文作为 prompt 交给子 Agent 运行即完成一次独立审查。 |
| `scan_secrets.py` | 密钥扫描器：扫描仓库内文本文件中的 API Key、口令、Token、私钥、带凭据的数据库 URL 等机密字面量。审查前**必须**运行。 |

## 快速使用

### 1. 密钥扫描（每次审查前必做）

```powershell
# 在仓库根目录
python reviewer/scan_secrets.py            # 全量扫描；退出码 0=干净，1=发现可疑项
python reviewer/scan_secrets.py --verbose  # 显示每条命中 file:line
python reviewer/scan_secrets.py --json     # JSON 报告（便于程序化处理）
python reviewer/scan_secrets.py --path backend  # 只扫某个子目录
```

规则说明：

- 覆盖 `sk-...`（DeepSeek/OpenAI）、`AKIA...`（AWS）、`ghp_...`（GitHub）、
  `AIza...`（Google）、`xoxb-...`（Slack）、私钥块、带凭据的数据库 URL、JWT、
  密钥类变量直接赋值等模式；
- 自动忽略 `.git`、`node_modules`、`.next`、`.venv`、`.deps`、构建产物等；
- `.env` 等真实环境变量文件会被标记为 HIGH（仅允许 `.env.example` 等占位模板）；
- 占位符值（`your-...`、`xxx`、`<...>`、`example` 等）不会被误报。

### 2. 触发审查

主 Agent 完成代码任务后：

1. 运行密钥扫描（见上）；
2. 把 `reviewer/REVIEWER_AGENT.md` 全文 + 本次变更清单交给一个独立的子 Agent
   （subagent）执行审查；
3. 审查员输出结构化报告：`APPROVED` / `APPROVED_WITH_COMMENTS` / `BLOCKED`；
4. 密钥相关结论一旦 `BLOCKED`，修复后必须重新扫描确认干净才可放行。

## 与项目规范的衔接

- 规范基线：`PROJECT_PLAN.md`（权威文档，第 23 节执行纪律）与 `README.md`。
- 配置规范：敏感配置通过 `backend/app/common/config.py` 的 `Settings`/`get_settings()`
  从环境变量读取，生产由云密钥管理服务注入；`backend/.env.example` 只保留占位符。
- 密钥文件 `.env*` 已在根 `.gitignore` 中忽略，审查时也会核对此项。
