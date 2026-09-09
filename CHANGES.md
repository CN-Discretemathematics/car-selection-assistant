# 更新说明（v2）

本文件记录 v2 相对 v1 的全部变更、向量库重建规则与验证结论。
技术选型与实测数据详见 `RAG_TECH_SELECTION.md` 与 `RAG.md` §4.2。

## 本次更新概要

1. **RAG 检索优化五连**（520 题四桶基准 + 权重消融驱动，详见 RAG.md §4.2）：
   款型级合并切片（Hit@5 +2.0pt）· 查询意图分流（参数/对比查询跳过稠密，省时省钱）·
   加权 RRF（网格消融定档 0.6/0.4，MRR 0.7032 为实测最优）· 重排器多协议适配
   （阿里百炼 qwen3 系 / SiliconFlow / Cohere 兼容，默认不重排亦为消融实测最优）·
   领域同义扩展 + 可选 HyDE
2. **稠密重建工程化**：embedding 向量 JSONL 流式缓存（断点续跑、低内存，修复
   2C2G 服务器全量驻留导致的 OOM）；insert 瞬态错误退避重试
3. **Agent 体验修复**：无在售款型车系的版本差异问答降级展示归档数据（标注停售），
   不再以「未收录」死胡同回应
4. **代码与文档清理**：删除 5 个一次性/被取代的工具脚本，清除全部指向内部文档的
   悬空引用，用例数与文档同步（189 用例）

## 向量库（Milvus/Zilliz）何时需要重建

| 改动类型 | 是否需要重建稠密索引 | 操作 |
| --- | --- | --- |
| Agent / 推荐 / 业务接口等**查询侧**代码 | ❌ 不需要 | 重启服务即可 |
| `chunking.py` / `ingest.py` 的**切片逻辑或元数据**变更 | ✅ 需要 | `python tools/build_retrieval_index.py --target dense` |
| 车型**数据变更**（新车型导入、参数/价格更新） | ✅ 需要（仅受影响部分也可全量重跑） | 同上（配 `RETRIEVAL_EMBED_CACHE` 可增量续跑） |
| 分词器 / BM25 / 融合权重 / 重排配置变更 | ❌ 不需要（只影响查询侧） | 重启服务；稀疏索引进程首用自动构建 |

云端操作只有一条命令（在部署目录执行，或用管理后台 `/ops/rag` 一键重建）：
`docker exec deploy-api-1 python tools/build_retrieval_index.py --target dense`

## 文件变更清单（v1 → v2，34 个文件）

### 新增（4）

- `RAG_TECH_SELECTION.md` — RAG 技术选型方案（每环节选型依据与实测数据）
- `backend/app/rag/synonyms.py` — 领域同义词扩展（查询理解，确定性无 LLM）
- `backend/app/retrieval/domain_words.txt` — 车圈领域词表（jieba 用户词典，100+ 词）
- `web/app/vehicles/[series_id]/page.tsx` — 按车系聚合的车型详情页

### 删除（5，均为一次性/被取代的工具）

- `backend/tools/fetch_wheels.py` — 旧依赖 vendoring 脚本（被 `pip install --target` + requirements.txt 取代）
- `backend/tools/replace_sample_data.py` — 一次性样例数据替换（已完成使命）
- `backend/tools/backfill_page_or_section.py` — 一次性数据回填
- `backend/tools/cleanup_orphan_accounts.py` — 一次性孤儿账户清理
- `backend/tools/register_sales_task.ps1` — Windows 计划任务注册（云端改用 crontab）

### 修改（25）

- 检索核心：`app/rag/{pipeline,rerank,ingest,service,state}.py`、`app/retrieval/{backends,config,zilliz}.py`
  —— 意图分流、加权 RRF、多协议重排适配、款型级切片、流式向量缓存、中文分词口径
- Agent：`app/agent/{engine,tools}.py`、`series_qa.py` —— 版本差异降级展示、解锁说明、引用
- 文档引用清理：`app/**`、`web/**` 共 60+ 文件的注释（移除指向内部文档的悬空引用）
- 测试：`test_rag.py`、`test_tools.py`、`test_zilliz.py` —— 新增/更新 12 个用例
- 工具：`eval_rag.py`（分桶报告 + 不重排对照组 + `--only`）、`gen_eval_questions.py`（520 题分桶）
- 配置：`.env.example`（RAG 新变量全量占位）、`requirements.txt`（+jieba）

## 验证结论

- `pytest` **189 passed**（新增 12 用例：加权 RRF / 意图分流 / 同义扩展 / 分词模式 /
  DashScope schema / 款型切片去重与配额 / 向量缓存断点续跑 / 归档车系降级展示）
- 密钥扫描：会入库口径干净（默认模式 exit 0）
- 云端实测（Zilliz 款型级集合 + 真实 RDS，60 题）：pipeline Hit@5 **0.7778**、
  NDCG@10 **0.7196**（全场最高）；加权 RRF 消融确认 0.6/0.4 最优
- 生产部署：阿里云 ECS Docker Compose 双容器运行中（api healthy），RDS 内网连接，
  alembic 自动迁移，销量每日定时抓取
