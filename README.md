# car-selection-assistant

基于真实车型数据的汽车选择推荐平台：**确定性筛选与评分**打底、**LLM 对话式交互**呈现，
所有车辆事实只来自数据库与工具返回值，并带来源引用与缺失标记（官方资料未披露）。

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11+-green)
![Next.js](https://img.shields.io/badge/Next.js-15-black)

## 功能特性

- **对话式选车 Agent**：预算 / 能源类型 / 车身形式 / 座位数 / 品牌等硬约束确定性解析；
  指定车系锁定与追问澄清；新硬约束与锁定车系冲突时自动解锁并**在同一次回复中告知**；
  盘点类问题（「奔驰都有哪些车型」）走确定性读库路径，不靠模型记忆；支持「新对话」
  一键重置会话记忆
- **确定性推荐引擎**：硬约束下推 SQL（宁可少推不可推错）、确定性评分、候选卡片带匹配置信说明
- **跨车系对比与差异分析**：SKU 级对比表（隐藏相同参数）之外，「分析差异」输出**决策相关的
  确定性结论**——先一句话结论与关键差异 Top3（按差距从大到小），全部维度明细默认折叠；
  并附 **LLM 一句话点评**（只复述库内事实、不得出现数字，失败自动回退确定性结论）；
  结论全部由库内事实推导，回答数字受白名单校验。**外部跳转入口只保留在车型详情页**
  （官方车型页优先，缺失时回退「查看数据来源」；对比场景不提供外链）
- **同车系版本差异问答**：按官方指导价列出在售款型、只列差异项并隐藏相同参数，
  部分款型独有的配置同样入列，缺失一律标注「官方资料未披露」；无在售款型的车系
  自动降级展示归档数据并标注停售
- **车系档案问答**：参数、配置、价格问答全部引用数据库事实与来源文档
- **能源类型严格口径**：BEV / PHEV / EREV / HEV / ICE SKU 级判定（厂商命名、纯电续航、
  电池能量、排量占位等多信号），避免「要燃油车却推插混」
- **RAG 检索（LangGraph）**：查询理解（实体解析 + 意图分流 + 同义扩展）→ BM25 ∥ 稠密
  并行召回 → **加权 RRF 融合** → 可插拔重排（按查询类型条件启用，A/B 实测）→ 证据把关；
  稠密后端支持进程内 / Milvus / Zilliz；`/ops/rag` 水位自证与可视化运维
- **评测驱动开发**：零人工标注黄金集自动生成（531 题 + 不可回答 60 + 多约束 80 +
  口语化改写 163，四桶分层），信息需求对齐的多口径指标（约束满足度 / 事实覆盖 /
  双侧覆盖），答案层 LLM-as-judge（双评一致率 0.913）+ 确定性数字溯源 faithful_db
  双度量；每个策略决策均有 A/B/消融数据，不符合预期的实现按数据回退
- **诚实性保障**：不可回答题 60/60 显式标注「官方资料未披露」（维度级 + 按键级），
  不编造、不沉默跳过
- **车系搜索**：导航栏 / 首页 / 移动端均可搜索，输入即出下拉结果（缩略图、品牌车系、
  指导价区间），↑↓ 选中、回车直达详情；匹配口径与 Agent 实体解析同源（车系名 / 品牌名 /
  别名，忽略大小写与空格，「z9gt」= 「腾势Z9 GT」=「腾势Z9GT」）
- **真实数据接入工具**：汽车之家销量榜、车系、SKU（参数配置/价格）三阶段抓取，断点续传，
  导入前 dry-run 校验、失败整体回滚
- **生产安全**：生产密钥经服务器 `.env` 注入（不入仓库、不入镜像，文件权限 600）；
  应用启动前运行 Alembic 迁移（幂等）
- **管理后台**：数据导入审计、数据质量冲突处理、RAG 运维

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2.0 · Alembic · Pydantic v2（SQLite 开发 / PostgreSQL 生产） |
| 会话与限流 | Redis（compose 内置实例；未配置时回退进程内实现，接口一致） |
| Agent | LLMClient 适配层（OpenAI 兼容 API；未配置 LLM 时自动降级为确定性回答） |
| RAG | LangGraph StateGraph · 进程内 BM25 稀疏 / Milvus / Zilliz 稠密 · 可插拔重排 |
| 前端 | Next.js 15 · React 19 · TypeScript · Tailwind CSS |

## RAG 检索：工作流与策略选型

### 端到端工作流

```text
真实数据抓取（销量榜 / 车系 / SKU，dry-run 校验 + 失败整体回滚）
  → 摄取流水线（LangGraph：load → chunk → index）
      ├─ 稀疏索引：进程内 BM25（bigram 分词 + 车系专名运行期注册）
      └─ 稠密索引：Zilliz 声明式集合（qwen3.7-text-embedding-flash · 1024 维 ·
         款型级切片 · 向量缓存断点续跑 · 重建成功率与水位可观测）
  → 月度销量入库 → flag 门控自动触发稠密增量重建（无新月度零成本跳过）
  → 查询流水线（analyze 实体解析/意图分流 → sparse ∥ dense → 加权 RRF → 条件重排 → 证据把关）
  → Agent 引用证据作答（带来源引用与缺失标记）
```

`/ops/rag` 可视化全程：流程图、**水位自证**（向量库构建时销量月份 vs 库内最新月份，
滞后自动告警）、重建进度、逐节点运行轨迹、评测报告。

### 策略选型及原因（同题库 A/B 实测，非经验拍板）

| 决策点 | 选型 | 实测依据（520/451 题基准） |
| --- | --- | --- |
| 切分粒度 | 款型级合并切片（事实按优先级合入 1~N 片，metadata 全量携带） | Hit@5 **+2.0pt**（0.6519 → 0.6718），命中即对齐 SKU |
| 稀疏分词 | bigram | jieba 整词 0.6563 / jieba+bigram 混合 0.6674，均低于 bigram 0.6718 |
| 召回融合 | BM25 ∥ 稠密并行 + 加权 RRF（0.6/0.4，k=60） | 稠密补语义洞（semantic 桶 MRR **+68%**）；等权 0.6741 < 加权；分路权重消融 0.6/0.4 最优 |
| 重排 | Cross-Encoder **按意图条件启用**（仅 semantic/recommend） | v13 全链路最优 0.6763/0.6656：实体锚定查询（parameter/compare，占 43%）上重排中性偏负且白付 ~1s/题 |
| HyDE | 关闭 | 154 题语义桶扩样 A/B：Hit@5 −1.3pt 且耗时 +4.4×，确认有害 |
| CHUNK_SIZE | 500 / overlap 64 | 400 → 0.6630、600 → 0.6585，均劣化 |
| 召回倍数 | 6（每路 top_k×6） | 8 无增益 |
| 稠密定位 | 补语义洞，不替代稀疏 | dense 单路 0.6452 < sparse 0.6718；hybrid 0.6741 |

### 检索质量（451 题 · 生产配置 · 真云 dense · 同题库复测）

| 指标 | 2026-09-09 基线 | 2026-09-11 工程重构后复测 |
| --- | --- | --- |
| Hit@5 | 0.6763 | **0.6763** |
| MRR | 0.6656 | **0.6656** |
| NDCG@10 | 0.6138 | **0.6138** |
| 端到端耗时 | 418.9s | **181.4s**（云端 API 时段波动为主，见 RAG.md §4.4） |

五个指标与四个分桶逐位一致——重构/迁移属于**零回归**改造。工程侧收益：

| 工程指标 | 优化前 | 优化后 |
| --- | --- | --- |
| 索引构建（load+chunk） | 数小时，连续失败（OOM / RDS 连接超时） | **5.1 秒**；峰值内存 ~1GB → **286MB** |
| 全量重建（12,078 条） | 无法完成 | 11 分钟；向量缓存命中后 **~2 分钟** |
| 向量库水位 | 不可见（曾静默落后一个月） | `/ops/rag` 自证 + 滞后自动告警 |

**评测规范 v2（信息需求对齐判定）**：旧口径把"一题多解"的推荐/语义查询按单锚点判定、
Recall@5 分母含相关车系全部切片（结构上限仅 0.4773，实际 0.2929 = 上限的 61%）。v2 按信息
需求对齐判定后，被掩盖的真实能力与短板同时现形：

| v2 指标（451 题复测） | sparse | pipeline（生产） |
| --- | --- | --- |
| fact-coverage@5（参数事实出现在证据中） | 0.7234 | **0.7660** |
| valid-hit@5（推荐/语义，约束满足即相关） | 0.4167 | **0.8432** |
| valid-precision@5 | 0.1731 | **0.8008** |
| pair-coverage@5（对比两侧证据在场） | 0.4390 | **0.5854** |

semantic 桶旧口径 Hit@5 0.1463 → v2 **1.0**：旧数字主要是口径失真（一题多解被记为不相关）；
新口径暴露的真实短板随后全部闭环——compare 两侧覆盖 0.439 → 0.5854（款型名解析修复；
跨车系撞名的通用款型名不再参与解析，宁可少答不错答，残差见 RAG.md §4.5）、参数事实
覆盖 0.766、recommend 约束有效率 0.5517 → 0.8008（约束下推 + 解析语义修复）。
评测规范详见 [RAG.md](RAG.md) §4.2。

**v3 扩充（不可回答 / 多约束 / 口语化改写）**：

- **拒答诚实性**：60 道不可回答题（锚定车系在该参数维度上完全无数据），修复前 0/60 →
  修复后 **60/60** 正确标注「官方资料未披露」——评测抓出并推动修复了一个真实产品 bug；
- **多约束推荐（80 题）**：旧口径 Hit@5 0.075 vs v2 valid-hit **0.65**（同题 8.7 倍差），
  valid-precision 0.445 是约束过滤优化的基准；
- **口语化改写（163 变体）**：参数题 fact-coverage −10pt、对比题 pair-coverage −9pt——
  词面泄漏被量化，鲁棒性优化自此有基准可对照。变体校验在**语义结构层**（与评测同源）：
  数字语义等价 + 实体必须经生产解析器解析到原车系（解析鲁棒率 **89.8%** 成为独立指标）+
  约束结构保持——表面形式不设限，真值不可恢复才拒绝。

**v4 检索侧闭环（针对 v2/v3 暴露的短板）**：

| 指标 | v4 前 | v4 后 |
| --- | --- | --- |
| valid-hit@5（推荐/语义，约束满足） | 0.7415 | **0.8136**（+7.2pt） |
| valid-precision@5 | 0.5517 | **0.6992**（+14.8pt） |
| valid-MRR | 0.6604 | **0.7607**（+10.0pt） |

机制：未点名车系的推荐/语义查询按解析出的硬约束**约束优先重排**（点名车系的问答
不受影响）。compare 双侧覆盖（v6.1 闭环）：名称索引扩展**在售款型显示名 → 车系**
（对比题以款型名表述，此前 50% 的对比题实体解析完全错位；跨车系撞名的通用款型名
不参与解析——宁缺毋滥），对比查询再按锚定车系各补一路过滤召回——pair-coverage
0.439 → 0.5854，compare hit@5 保持 0.99，其余指标零回归。

**答案层评测（LLM-as-judge + 确定性 faithful_db）**：

- judge 与作答模型分离（作答 DeepSeek、judge 独立模型），100 题分层抽样：双评一致率
  **0.913**、semantic 桶 faithful **1.0**、recommend 0.80；
- **faithful_db（确定性数字溯源，零 judge 成本）0.7765**：回答中的每个数字必须能追溯到
  锚定车系的 DB 数据（事实值/单位/指导价/销量/款型名）或问题本身——参数/对比类回答由
  模板直接从全量 DB 事实生成（带引用），judge 对照 RAG top-5 证据会系统性低估其忠实度，
  数字溯源才是这类回答的正确忠实度度量；
- 拒答诚实性 **60/60**：不可回答题全部显式标注「官方资料未披露」（维度级 + 按键级），
  不编造、不沉默跳过。

逐轮消融数据与设计细节见 [RAG.md](RAG.md)。

## 快速开始

### 后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # bash: source .venv/bin/activate
pip install -r requirements.txt
Copy-Item .env.example .env           # bash: cp .env.example .env
# 编辑 .env：DATABASE_URL 默认 SQLite 即可跑通；DEEPSEEK_API_KEY 留空则确定性回答
alembic upgrade head
python -m uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd web
pnpm install
pnpm dev                              # http://localhost:3000
```

### 导入车型数据（可选）

导入真实数据后才能体验完整功能（空库会返回「暂无数据」类回答）：

```powershell
cd backend
# 销量榜（某月，门户口径）
python tools/fetch_autohome_sales.py --month 2026-07 --do-import
# 全量车系与 SKU（index / series / sku 三阶段，断点续传；--no-import 只抓不导）
python tools/fetch_autohome_sku.py --stage index
# 校验导入载荷（先 dry-run，失败整体回滚）
python tools/import_data.py payload.json --dry-run
```

数据抓取请遵守目标站点 robots.txt 与服务条款；本项目仅抓取公开页面（门户口径），
仅供学习研究使用。

## 环境变量

复制 `backend/.env.example` 后按需填写，全部有默认值，**零配置可跑通开发模式**：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | 默认 SQLite；生产用 `postgresql+psycopg://...` |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | OpenAI 兼容 LLM；留空 = 确定性回答 |
| `REDIS_URL` | 会话 / 限流 / 验证码存储；留空 = 进程内实现 |
| `RETRIEVAL_BACKEND` | RAG 稠密召回后端：`inmemory`（默认）/ `milvus` |
| `RETRIEVAL_TOKENIZER` | 稀疏分词器：`bigram`（默认）/ `jieba` / `hybrid` |
| `RERANK_PROVIDER` | 重排器：空（默认，保持融合序）/ `lexical` / `api` |
| `ADMIN_API_TOKEN` / `ADMIN_API_TOKENS` | 管理后台凭据：后者支持 `label:token` 逗号分隔多标签（可单独吊销、审计区分操作者），旧单值变量兼容（标签 `legacy`）；留空 = 管理接口禁用 |
| `OSS_*` | 网页快照 / 图片原始文件存储（可选） |

## 测试

```powershell
cd backend
python -m pytest -q                   # 518 用例（随迭代增长，以实际输出为准）
cd ..\web
npx tsc --noEmit                      # 前端类型检查
```

## 项目结构

```
backend/
  app/
    admin/        # 管理后台 API（数据导入审计 / 质量冲突 / RAG 运维）+ 操作审计中间件
    agent/        # 对话引擎：约束解析、车系锁定与追问、版本差异问答、对比差异分析
    auth/         # 注册 / 登录 / 验证码（Redis 或进程内存储）
    brands/       # 品牌注册表与品牌盘点
    catalog/      # 车系索引与锁定
    common/       # 配置 / 数据库 / LLMClient 适配层 / 模型定义 / 限流 / Redis 客户端
    comparison/   # 跨车系对比（内容哈希幂等）+ 确定性差异分析
    images/       # 图片代理（域名白名单）
    rag/          # LangGraph 双流水线（查询 / 摄取）+ 同义扩展
    recommendation/  # 确定性推荐（硬约束下推 SQL + 评分）
    retrieval/    # BM25 / 稠密召回后端、分词器、重排配置、领域词表
    sales/        # 月度销量接口
    sources/      # 数据源适配与导入（汽车之家 SKU、能源类型判定）
    vehicles/     # 车型查询 / 车系搜索接口
    variants/     # 参数归一化（单位 / 工况 / 缺失值）
  alembic/        # 数据库迁移
  tools/          # 数据抓取 / 导入 / 评测 / 运维脚本
  tests/          # pytest 用例
web/
  app/            # Next.js 页面（首页 / 车型列表与详情 / 对比 / 收藏 / 隐私与 AI 声明 / ops 运维）
  lib/            # API 客户端、Agent 触发词、认证与筛选工具
skills/           # 开发工作流沉淀（数据校验 / 参数归一化 / 端到端验证 / 文档同步等）
deploy/           # Docker Compose、前后端 Dockerfile、Nginx 配置、自动部署 / 夜间任务 / 缺口回补脚本
reviewer/         # 独立代码审查 Agent（密钥扫描器 + UI 文案门禁 + 审查规范）
docs/             # 运维手册（部署与自动更新 / 阿里云控制台运维 / 凭据轮换）
RAG.md            # RAG 子系统设计与运维文档（技术选型对比见 RAG_TECH_SELECTION.md）
```

## 生产部署（Docker Compose + Nginx）

> 完整手册见 [docs/deployment.md](docs/deployment.md)（服务器实际形态、自动更新、回滚、新机接入）；
> 阿里云侧运维（续费 / 防火墙 / 快照 / RDS 账号与备份恢复 / OSS / RAM / 域名备案）见
> [docs/aliyun-ops.md](docs/aliyun-ops.md)。

```bash
# 服务器（阿里云中国内地节点；容器内跑 redis / api / web 三服务）
# 注意：实际生产机不装 git，源码经 GitHub 源码包同步（见 docs/deployment.md）
git clone <repo> /srv/carsel && cd /srv/carsel
cp backend/.env.example backend/.env      # 填生产值（数据库 / LLM / 嵌入 / 管理凭据）
cd deploy
docker compose up -d --build              # 构建并启动
cp nginx.conf /etc/nginx/conf.d/carsel.conf   # server_name 改为已备案域名
nginx -t && systemctl reload nginx
```

**自动更新（只部署 main）**：`deploy/carsel-deploy.sh` 每天 05:00 由 cron 触发——
对比 GitHub 上 `main` 的最新提交，有更新才同步源码、重建镜像、切流，并在健康检查失败时
自动回滚到上一版镜像。手工执行 `carsel-deploy.sh --check / --dry-run / --force` 可查状态、
预演与强制重部署。

部署要点（均为线上实证）：

- **容器只绑定 `127.0.0.1`**，外部统一经宿主 nginx 进入；`/api` 由 nginx **直连后端**
  （经 Next.js rewrite 转发会让 Agent 长请求挂死），SSE 端点关缓冲
- **国内构建**：Docker 需配 `registry-mirrors`；前端 npm 走 npmmirror（`frontend.Dockerfile`
  内固化为 `/root/.npmrc`），运行时直接调用镜像内 `next` 二进制，无运行期联网依赖
- **数据库**：容器化部署与 RDS 不同 VPC 时须使用**外网 endpoint**，并把出口 IP 加入白名单
- **定时任务**：看门狗（每 5 分钟自愈）+ 夜间 02:30 销量导入 → 条件触发稠密重建
  + 早上 05:00 自动部署 `main`
- **备案**：中国内地节点须完成 ICP 备案；页脚备案号由环境变量 `ICP_NUMBER` 注入并链接工信部；
  备案通过前对外只能用 IP 访问（`http://121.41.4.12`），域名在大陆节点对外服务属违规

## 安全实践

**凭据与密钥**
- 密钥只经服务器 `.env`（权限 600）或平台环境变量注入；仓库只保留 `.env.example` 占位模板
- `.dockerignore` 从构建上下文排除 `.env` / 私钥 / 本地数据——密钥不进镜像层与构建缓存
- 仓库自带三道静态门禁：密钥扫描器（`reviewer/scan_secrets.py`）、UI 文案门禁
  （`reviewer/scan_ui_copy.py`，拦截用户界面的内部术语与实现说明、以及全局 footer 文案
  在页面正文重复）、文档一致性检查（`skills/doc_sync_check.py`，拦截用例数等计数漂移、
  悬空路径引用、配置表缺项与编码损坏）；三者均已接入 CI，`.gitignore` 覆盖
  `*.pem` / `*.key` 等私钥模式
- 轮换流程见 [docs/credential-rotation.md](docs/credential-rotation.md)（先建新 → 再切换 → 最后废旧）；
  轮换后用 `deploy/verify_credentials.py` 在容器内跑六类凭据的只读验收

**访问控制**
- 管理后台独立 Bearer 凭据（CSPRNG 随机值，由运维注入），不开放注册；支持多标签
  `ADMIN_API_TOKENS`（`label:token` 逗号分隔，可单独吊销、审计区分操作者；旧单值
  `ADMIN_API_TOKEN` 兼容，标签 `legacy`）；缺失返回 503、无效返回 401，
  比较使用 `secrets.compare_digest`（常量时间）
- 交互式 API 文档（`/docs`、`/openapi.json`）**默认关闭**（`DOCS_ENABLED=true` 可开）——它们会枚举全部管理端点
- 限流按**真实客户端 IP**：容器以 `--proxy-headers --forwarded-allow-ips=127.0.0.1` 启动，
  只信任本机 nginx 传来的 `X-Forwarded-For`（不可用 `*`，否则可伪造）
- 认证令牌与验证码：`token_urlsafe(32)`、6 位 CSPRNG、5 次尝试上限、登录/注册不区分邮箱是否已注册

**容器与网络**
- 容器端口只绑 `127.0.0.1`；Redis 不发布端口
- 容器加固：`cap_drop: ALL`、`no-new-privileges`、内存与进程数上限
- 健康检查用 `/api/v1/ready`（真实探测数据库），与 `/api/v1/health` 存活探针分离

**数据面**
- 车辆事实只来自数据库与工具返回值，回答附来源引用与缺失标注（LLM 无写权限、无执行工具）
- 数据库访问全部参数化（无字符串拼接 SQL）；图片代理为域名白名单（拒绝内网/元数据地址）
- 会话 ID 为 122 位随机值；CORS 非通配

> 已知待办：HTTPS/HSTS（备案完成后配置）、容器非 root 运行、RAG 数据面的并发与优雅降级加固。

## 设计原则

1. **事实只来自数据库与工具返回值**：LLM 不生成车辆参数，回答附来源引用与缺失标记
2. **硬约束确定性执行**：筛选与推荐不走「模型自由发挥」，宁可少推不可推错
3. **密钥不落仓库**：`.env` 一律 gitignore，仓库只保留占位模板
4. **本地无云依赖可运行**：SQLite + 进程内实现即可完成开发与测试闭环

## License

[Apache-2.0](LICENSE)
