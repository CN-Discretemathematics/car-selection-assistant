# car-selection-assistant

基于真实车型数据的汽车选择推荐平台：**确定性筛选与评分**打底、**LLM 对话式交互**呈现，
所有车辆事实只来自数据库与工具返回值，并带来源引用与缺失标记（官方资料未披露）。

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11+-green)
![Next.js](https://img.shields.io/badge/Next.js-15-black)

## 功能特性

- **对话式选车 Agent**：预算 / 能源类型 / 车身形式 / 座位数等硬约束确定性解析；指定车系
  锁定与追问澄清；新硬约束与锁定车系冲突时自动解锁并**在同一次回复中告知**
- **确定性推荐引擎**：硬约束下推 SQL（宁可少推不可推错）、确定性评分、候选卡片带匹配置信说明
- **同车系版本差异问答**：按官方指导价列出在售款型、只列差异项并隐藏相同参数，
  部分款型独有的配置同样入列，缺失一律标注「官方资料未披露」；无在售款型的车系
  自动降级展示归档数据并标注停售
- **车系档案问答**：参数、配置、价格问答全部引用数据库事实与来源文档
- **能源类型严格口径**：BEV / PHEV / EREV / HEV / ICE SKU 级判定（厂商命名、纯电续航、
  电池能量、排量占位等多信号），避免「要燃油车却推插混」
- **RAG 检索（LangGraph）**：查询理解（实体解析 + 意图分流 + 同义扩展）→ BM25 ∥ 稠密
  并行召回 → **加权 RRF 融合** → 可插拔重排（按查询类型条件启用，A/B 实测）→ 证据把关；
  稠密后端支持进程内 / Milvus / Zilliz；`/ops/rag` 水位自证与可视化运维
- **真实数据接入工具**：汽车之家销量榜、车系、SKU（参数配置/价格）三阶段抓取，断点续传，
  导入前 dry-run 校验、失败整体回滚
- **管理后台**：数据导入审计、数据质量冲突处理、RAG 运维

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2.0 · Alembic · Pydantic v2（SQLite 开发 / PostgreSQL 生产） |
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
| HyDE | 关闭 | 消融臂与基线持平，无增益 |
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
| valid-hit@5（推荐/语义，约束满足即相关） | 0.4167 | **0.7885** |
| valid-precision@5 | 0.1731 | **0.6992** |
| pair-coverage@5（对比两侧证据在场） | 0.4390 | **0.7073** |

semantic 桶旧口径 Hit@5 0.1463 → v2 **1.0**：旧数字主要是口径失真（一题多解被记为不相关）；
新口径暴露的真实短板随后全部闭环——compare 两侧覆盖 0.439 → **0.7073**（款型名解析修复，
见 v4/v6.1）、参数事实覆盖 0.766（款型级切片 + 按需参数查找直读 DB）、recommend 约束有效率
0.5517 → 0.6992（约束下推）。评测规范详见 [RAG.md](RAG.md) §4.2。

**v3 扩充（不可回答 / 多约束 / 口语化改写）**：

- **拒答诚实性**：60 道不可回答题（锚定车系在该参数维度上完全无数据），修复前 0/60 →
  修复后 **60/60** 正确标注「官方资料未披露」——评测抓出并推动修复了一个真实产品 bug；
- **多约束推荐（80 题）**：旧口径 Hit@5 0.075 vs v2 valid-hit **0.65**（同题 8.7 倍差），
  valid-precision 0.445 是约束过滤优化的基准；
- **口语化改写（167 变体）**：参数题 fact-coverage −10pt、对比题 pair-coverage −9pt——
  词面泄漏被量化，鲁棒性优化自此有基准可对照。

**v4 检索侧闭环（针对 v2/v3 暴露的短板）**：

| 指标 | v4 前 | v4 后 |
| --- | --- | --- |
| valid-hit@5（推荐/语义，约束满足） | 0.7415 | **0.8136**（+7.2pt） |
| valid-precision@5 | 0.5517 | **0.6992**（+14.8pt） |
| valid-MRR | 0.6604 | **0.7607**（+10.0pt） |

机制：未点名车系的推荐/语义查询按解析出的硬约束**约束优先重排**（点名车系的问答
不受影响）。compare 双侧覆盖（v6.1 闭环）：名称索引扩展**在售款型显示名 → 车系**
（对比题以款型名表述，此前 50% 的对比题实体解析完全错位），对比查询再按锚定车系
各补一路过滤召回——pair-coverage 0.439 → **0.7073**，compare hit@5 保持 0.9873，
NDCG@10 0.5447 创新高，其余指标零回归。

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
| `OSS_*` | 网页快照 / 图片原始文件存储（可选） |

## 测试

```powershell
cd backend
python -m pytest -q                   # 200 用例
cd ..\web
npx tsc --noEmit                      # 前端类型检查
```

## 项目结构

```
backend/
  app/
    agent/        # 对话引擎：约束解析、车系锁定与追问、版本差异问答
    catalog/      # 车系索引与锁定
    comparison/   # 跨车系对比（内容哈希幂等）
    rag/          # LangGraph 双流水线（查询 / 摄取）+ 同义扩展
    recommendation/  # 确定性推荐（硬约束下推 SQL + 评分）
    retrieval/    # BM25 / 稠密召回后端、分词器、重排配置、领域词表
    sources/      # 数据源适配与导入（汽车之家 SKU、能源类型判定）
    vehicles/     # 车型查询接口
    variants/     # 参数归一化（单位 / 工况 / 缺失值）
  alembic/        # 数据库迁移
  tools/          # 数据抓取 / 导入 / 评测 / 运维脚本
  tests/          # pytest 用例
web/
  app/            # Next.js 页面（选车 / 对比 / 详情 / 管理后台 / ops）
  lib/            # API 客户端与触发词
skills/           # 开发工作流沉淀（数据校验 / 参数归一化 / 端到端验证）
RAG.md            # RAG 子系统设计与运维文档
```

## 设计原则

1. **事实只来自数据库与工具返回值**：LLM 不生成车辆参数，回答附来源引用与缺失标记
2. **硬约束确定性执行**：筛选与推荐不走「模型自由发挥」，宁可少推不可推错
3. **密钥不落仓库**：`.env` 一律 gitignore，仓库只保留占位模板
4. **本地无云依赖可运行**：SQLite + 进程内实现即可完成开发与测试闭环

## License

[Apache-2.0](LICENSE)
