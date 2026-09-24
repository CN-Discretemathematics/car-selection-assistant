# Reviewer（代码审查 Agent）

本目录是 carSelection 项目的**独立代码审查 Agent** 配套文件，用于在主 Agent 完成
代码任务后执行代码审查，确保代码规范，**最优先保障：任何 API Key / 机密不得进入
会被上传或提交的文件**。

## 目录内容

| 文件 | 用途 |
|---|---|
| `REVIEWER_AGENT.md` | 审查员身份定义、密钥红线、触发时机、审查流程、规范清单、报告模板。主 Agent 完成任务后，将本文件全文作为 prompt 交给子 Agent 运行即完成一次独立审查。（该文件按 `.gitignore:68` 的约定**只保留在本地**，新克隆不可见。） |
| `scan_secrets.py` | 密钥扫描器：扫描仓库内文本文件中的 API Key、口令、Token、私钥、带凭据的数据库 URL 等机密字面量。审查前**必须**运行。 |
| `scan_ui_copy.py` | 用户可见文案门禁：拦「内部术语/实现说明泄漏到界面」与「全局 footer 文案在页面正文重复一遍」。审查前**必须**运行。 |

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
- `.env` 等真实环境变量文件会被标记为 HIGH（仅允许 `.env.example` 等占位模板），
  位于 gitignored 路径的命中按 `[INFO]` 呈现、不计入退出码；
- 占位符值（`your-...`、`xxx`、`<...>`、`example` 等）不会被误报。

### 2. 用户可见文案门禁（每次审查前必做）

```powershell
python reviewer/scan_ui_copy.py            # 退出码 0=干净，1=发现文案问题
python reviewer/scan_ui_copy.py --verbose  # 逐条打印命中位置
```

拦两类东西：

1. **内部表述**：术语（SKU / §章节号 / 门户口径 / 范围内 / 幂等 / 落库…）与实现说明措辞
   （「不做猜测补全」「统一显示」等）。前端扫公开页面（`/ops` 与仅它使用的
   `web/lib/rag.ts` 除外）；后端用 `ast` 只看**非 docstring 的字符串字面量**——注释与
   docstring 里的技术术语是给维护者的，允许保留。
2. **footer 文案重复**：以全局 footer 为唯一真源（默认 `web/app/layout.tsx`，实际按 `<footer`
   标签自动定位，所以拆成 `SiteFooter.tsx` 也认），把它切成子句后回查其余前端文件，逐字重复
   即 FAIL（判「同一句话出现两遍」，不做近义改写判断；长度 < 8 字的短词如「隐私政策」不判，
   避免噪声）。**定位不到唯一 footer 时退出码 2**——「无法判定」不等于「通过」，否则重构后
   门禁会静默空转。

**三次事故对应两条规则**：① 2026-09-14 详情页脚注整句实现说明 + 后端错误信息里的内部叫法，
漏过第一轮固定术语表；② 2026-09 首页与详情页把 footer 的免责/AI 标识又说了一遍。前两次
都是用户看到才发现；② 之后补上第 2 条检查，并由 `backend/tests/test_ui_copy_gate.py`
的 footer 用例 + 变异测试守着。

### 3. 触发审查

主 Agent 完成代码任务后：

1. 运行密钥扫描（见上）；
2. 把 `reviewer/REVIEWER_AGENT.md` 全文 + 本次变更清单交给一个独立的子 Agent
   （subagent）执行审查；
3. 审查员输出结构化报告：`APPROVED` / `APPROVED_WITH_COMMENTS` / `BLOCKED`；
4. 密钥相关结论一旦 `BLOCKED`，修复后必须重新扫描确认干净才可放行。

## 与项目规范的衔接

- 规范基线：`README.md`（设计原则与安全实践）与 `AGENTS.md`（执行纪律、并行会话规范、验证基线）。
- 配置规范：敏感配置通过 `backend/app/common/config.py` 的 `Settings`/`get_settings()`
  从环境变量读取，生产由云密钥管理服务注入；`backend/.env.example` 只保留占位符。
- 密钥文件 `.env*` 已在根 `.gitignore` 中忽略，审查时也会核对此项。
