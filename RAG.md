# RAG 设计与运维（LangGraph 版）

> 本文是 RAG 子系统的实现级文档：架构、策略选型依据、评估方法、可视化与运维入口。
> 全局原则：事实只来自数据库、密钥不落仓库、本地无云依赖可运行。

## 1. 总览

RAG 编排基于 **LangGraph**（`StateGraph`），拆成两条流水线，代码位于 `backend/app/rag/`：

| 流水线 | 模块 | 节点 | 触发 |
| --- | --- | --- | --- |
| 查询（在线） | `pipeline.py` | analyze → recall_sparse ∥ recall_dense → fuse → rerank → grade | Agent 工具 `retrieval_search`、管理后台试运行 |
| 摄取（离线） | `ingest.py` | load → chunk → index | `tools/build_retrieval_index.py`、管理后台重建索引、开发模式数据变更自动重建 |

召回后端与向量库客户端保留在 `backend/app/retrieval/`（`backends.py` BM25、`zilliz.py` Milvus/Zilliz REST + OpenAI 兼容 embedding）。`app/rag/service.py` 是唯一门面：`search()` / `try_query()` / `run_reindex()` / `get_status()` / `recent_runs()` / `graph_spec()`。

依赖：`langgraph>=1.2`（`requirements.txt`；沙箱环境经 `tools/fetch_wheels.py` vendor 化，含 langchain-core / langsmith / langgraph-checkpoint 等完整依赖树）。

## 2. 查询流水线

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	analyze(analyze)
	recall_sparse(recall_sparse)
	recall_dense(recall_dense)
	fuse(fuse)
	rerank(rerank)
	grade(grade)
	__end__([<p>__end__</p>]):::last
	__start__ --> analyze;
	analyze -.-> __end__;
	analyze -.-> recall_dense;
	analyze -.-> recall_sparse;
	fuse --> rerank;
	recall_dense --> fuse;
	recall_sparse --> fuse;
	rerank --> grade;
	grade --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

> mermaid 源码由 LangGraph `draw_mermaid()` 自动生成，与代码永远一致：
> `GET /api/v1/admin/rag/graph` 或 web `/ops/rag` 流程图页签。

各节点职责与主流策略对照：

1. **analyze（查询理解）**：用 `app/catalog/series_index.py` 的名称索引（车系名/品牌+车系名/别名，归一化子串匹配、最长优先去重）解析消息中的真实车系；生成实体增强查询（别名 → 规范名）与元数据过滤（单车系自动加 `series_id`）。这是「query understanding + metadata extraction」的确定性实现，不依赖 LLM，可复现。空查询在此直接短路到 END。
2. **recall_sparse ∥ recall_dense（并行多路召回）**：LangGraph 条件边扇出两个分支并行召回，每路取 `top_k × RETRIEVAL_RECALL_MULTIPLIER`（默认 6，下限 20、上限 100）。
   - sparse：进程内 Okapi BM25（k1=1.5, b=0.75），中英文混合分词（英文/数字词元 + 汉字二元组），始终可用；
   - dense：Zilliz Cloud REST v2 向量检索（cosine，autoindex），`RETRIEVAL_BACKEND=milvus` 且 URI/Token 齐备时启用；未配置空转、调用失败降级为纯稀疏并记 warning（原则 7）。
3. **fuse（RRF 融合）**：Reciprocal Rank Fusion（k=60，业界默认）。两路分数量纲不可比（BM25 分 vs 余弦距离），RRF 只用名次、免调参，是多路融合的标准做法（Milvus WeightedRanker/RRF、Elasticsearch RRF 同思路）。按 `chunk_id` 去重（Zilliz meta 存原始 chunk_id；旧集合回退文本哈希），再按文本兜底去重。
4. **rerank（精排）**：可插拔重排器（`rerank.py`）：
   - `CrossEncoderReranker`（`RERANK_PROVIDER=api`）：Cross-Encoder 重排服务，兼容硅基流动（BAAI/bge-reranker-v2-m3）/ Jina / Cohere 的 `POST {base}/rerank` 协议；返回绝对相关分；
   - `LexicalReranker`（默认）：查询-切片词元重叠加权（复用 BM25 的 tokenize，加分 `min(重叠×0.05, 0.25)`），零外部依赖，缓解「语义近但实体错」噪声；
   - API 重排失败自动回退 lexical 并记 warning，检索不中断。
5. **grade（证据把关）**：corrective-RAG 思路的轻量实现——丢弃空文本证据；重排分为绝对相关分（Cross-Encoder）时应用 `RETRIEVAL_RELEVANCE_THRESHOLD` 阈值过滤低相关证据。 lexical 分是相对名次分，不做阈值化。

每次运行记录阶段轨迹（节点、耗时、输入/输出数量、detail、warnings）到进程内环形缓冲（`RAG_RUN_LOG_SIZE`，默认 50），供管理后台「运行轨迹」可视化。

## 3. 摄取流水线

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	load(load)
	chunk(chunk)
	index(index)
	__end__([<p>__end__</p>]):::last
	__start__ --> load;
	chunk -.-> __end__;
	chunk -.-> index;
	load --> chunk;
	index --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

- **load**：SQL 装载原料——活跃车系、按车系分层取样的在售 SKU 事实（`row_number() over (partition by series)`，核心参数优先级分组，**过采样 ×5 + (车系,键,值,单位) 去重后再截配额**——同键同值跨款重复行不再吃光配额，每车系入索引 `RETRIEVAL_FACTS_PER_SERIES` 条**去重后**事实）、车系画像聚合（价格区间/在售数/最新月销量）。
- **chunk**：构造四类切片（元数据符合 §16.3）：
  | kind | chunk_id | 内容 |
  | --- | --- | --- |
  | `series_intro` | `series-{id}` | 品牌+车系+定位+能源类型 |
  | `series_summary` | `summary-{id}` | 车系级一句话画像：定位/指导价/核心参数/在售数/月销量（车系级问题的最优命中目标） |
  | `spec_fact` | `fact-{id}` | 结构化事实：全系统一值 → 车系级表述（不带款型名），同键多值 → 保留款型名逐款切片；噪声值「暂无/优惠信息」跳过；单位去重 |
  | `source_document` | `doc-{id}#{i}` | 来源文档正文，**递归字符切分**（`chunking.py`：段落→行→中文句读→英文句读→空格→字符级硬切；`CHUNK_SIZE=500`、`CHUNK_OVERLAP=64`，重叠窗口防止句界证据割裂——LangChain RecursiveCharacterTextSplitter 同款语义） |
- **index**：写入目标后端（`target=sparse|dense`；dense 走批量 embedding + Zilliz upsert，幂等重建集合，限流自动指数退避）。`build_chunks()` 复用同一条图的 load+chunk 两节点（`target=""` 时 index 被条件边跳过）。

索引新鲜度：开发模式（inmemory）按数据量快照自动重建；生产（milvus）稀疏索引进程首用构建、稠密索引显式重建（工具或管理后台），避免请求路径上的全量 embedding。

## 4. 评估（主流指标 + 黄金集）

`tools/eval_rag.py`：黄金集来自 `tools/gen_eval_questions.py`（数据库真实品牌/车系/SKU 分层抽样，`anchors.series_id/variant_id` 即相关性判定，无人工标注成本）。

- 指标：**HitRate@K、Recall@K、Precision@K、MRR、NDCG@K**（二元相关，IDCG 按相关集大小截断）。
- 策略对比（量化每个环节的边际收益）：
  - `sparse-nooverlap` vs `sparse` → **切分**（重叠窗口）收益；
  - `sparse` vs `dense` → **召回**路对比；
  - `hybrid`（sparse+dense+RRF）− max(sparse, dense) → **融合**收益；
  - `pipeline`（实体解析 + 重排 + 把关全链路）− hybrid → **重排/查询理解**端到端收益。
- 附切分质量报告：切片数、长度分布（mean/p50/p95/max）、超长占比、过短占比。
- 产物：`eval/rag_eval_report.json` + `.md`（管理后台「评测报告」页签直接读取）。

```powershell
cd backend
python tools/eval_rag.py --limit 60              # 本地：sparse 系策略 + pipeline
python tools/eval_rag.py --with-dense            # 加测 dense/hybrid（产生 Zilliz/embedding 云端调用）
```

Agent 端到端评测（硬约束零违规 + 引用校验 + hit@k）仍由 `tools/eval_agent.py` 承担，两者互补：eval_rag 看检索层，eval_agent 看业务层。

### 4.1 评测结果（真实数据，60 题抽样）

> 最新报告见 `eval/rag_eval_report.md`；A = 索引覆盖优化后（2026-09，纯稀疏三策略），
> B = 优化前同黄金集对照（含 Zilliz 云端召回路）。

**A. 覆盖优化后（spec_fact 切片 33,465：去重 ×5 过采样 + 每车系配额 40）**

| 策略 | Hit@5 | MRR | NDCG@10 | 耗时 |
| --- | --- | --- | --- | --- |
| sparse-nooverlap | 0.7609 | 0.7435 | 0.6750 | 2.3s |
| sparse（BM25） | 0.7609 | 0.7435 | 0.6750 | 2.4s |
| pipeline（纯稀疏全链路） | 0.7391 | 0.7391 | 0.6839 | 19.8s |

**B. 优化前对照（spec_fact 17,489；含 dense/hybrid 云端实测）**

| 策略 | Hit@5 | MRR | NDCG@10 |
| --- | --- | --- | --- |
| dense（Zilliz 向量） | 0.7174 | 0.7031 | 0.5899 |
| hybrid（RRF 融合） | 0.7609 | 0.7120 | 0.6355 |
| pipeline（当时全链路） | 0.7174 | 0.6915 | 0.6783 |

解读：
- **索引覆盖优化的直接收益**：同为纯稀疏路，Hit@5 73.9% → 76.1%、MRR 0.7205 → 0.7435——此前同键同值跨款重复行吃光每车系 20 条配额，续航/油耗等高频参数根本没进索引（实测「续航多少」检索为空）；修复后纯稀疏已追平优化前 hybrid 的成绩；
- **RRF 融合仍保留**：dense 语义召回兜底无实体表述的查询（「适合家庭」类），生产 `RETRIEVAL_BACKEND=milvus` 下并行召回 + RRF 融合是最终形态；
- sparse 与 sparse-nooverlap 同分：当前 RDS 库来源文档 `content_text` 为空（source_document 切片为 0），重叠切分收益暂无从体现；文档正文入库后应复测；
- pipeline 略低于裸 sparse 但 NDCG@10 更高——查询理解/重排优化的是整体排序质量与证据纯度，而非单点命中；
- dense 单路耗时 ~0.5s/题（embedding + 云端检索），线上由并行召回掩盖（总延迟 ≈ max 而非 sum）。

## 5. 流程管理可视化

- **web 页面**：`/ops/rag`（`web/app/ops/rag/page.tsx`，管理凭据入口）
  - 流程图：两条 LangGraph 流水线的节点图（条件边标注）+ mermaid 源码；
  - 运行状态：后端模式、稀疏/稠密索引、重排器、策略参数、数据库规模；一键重建索引（dense 后台任务 + 进度轮询）；
  - 试运行：单条查询的阶段瀑布（耗时条形、输入→输出数量、detail 展开）+ 命中证据卡片；
  - 运行轨迹：最近 50 次流水线运行记录（警告、dense 参与标记）；
  - 评测报告：eval_rag / eval_agent 结果表格。
- **管理 API**（`app/admin/rag_router.py`，全部 `require_admin`）：
  ```text
  GET  /api/v1/admin/rag/graph              # 节点/边/mermaid
  GET  /api/v1/admin/rag/status             # 索引与策略状态（无密钥）
  GET  /api/v1/admin/rag/runs?limit=20      # 运行轨迹
  POST /api/v1/admin/rag/query              # 试运行 {query, filters?, top_k}
  POST /api/v1/admin/rag/reindex            # {target: sparse|dense}
  GET  /api/v1/admin/rag/reindex-progress   # dense 重建进度
  GET  /api/v1/admin/rag/eval               # 评测报告
  ```
- **LangSmith（可选）**：依赖树已含 langsmith；配置 `LANGSMITH_API_KEY` / `LANGSMITH_TRACING=true` 后 LangGraph 自动上报 trace，可在 LangSmith / LangGraph Studio 查看逐节点运行详情。未配置时零影响。

## 6. 配置参考（backend/.env）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RETRIEVAL_BACKEND` | `inmemory` | `milvus` 时启用稠密召回路 |
| `MILVUS_URI` / `MILVUS_TOKEN` / `MILVUS_COLLECTION` / `MILVUS_DIM` | - | Zilliz Cloud（生产经 KMS 注入） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_DIMENSIONS` | - | OpenAI 兼容 `/v1/embeddings`（推荐阿里百炼 text-embedding-v3） |
| `RETRIEVAL_CHUNK_SIZE` / `RETRIEVAL_CHUNK_OVERLAP` | 500 / 64 | 递归切分参数（字符） |
| `RETRIEVAL_RECALL_MULTIPLIER` | 6 | 每路召回 = top_k × N（20~100 截断） |
| `RETRIEVAL_FACTS_PER_SERIES` | 40 | 每车系入索引的去重后事实条数（配合 ×5 过采样） |
| `RETRIEVAL_MAX_CHUNKS` | 60000 | 全库切片上限 |
| `RETRIEVAL_RRF_K` | 60 | RRF 平滑常数 |
| `RERANK_PROVIDER` | 空（lexical） | `api` 启用 Cross-Encoder 重排 |
| `RERANK_BASE_URL` / `RERANK_MODEL` / `RERANK_API_KEY` / `RERANK_TIMEOUT_SECONDS` | - | 如硅基流动 `https://api.siliconflow.cn` + `BAAI/bge-reranker-v2-m3` |
| `RETRIEVAL_RELEVANCE_THRESHOLD` | 0（关） | >0 时仅对 Cross-Encoder 绝对分生效 |
| `RETRIEVAL_LEXICAL_BOOST` / `RETRIEVAL_LEXICAL_CAP` | 0.05 / 0.25 | lexical 重排加分步长/封顶 |
| `RAG_RUN_LOG_SIZE` | 50 | 运行轨迹环形缓冲长度 |

## 7. 运维手册（速查）

```powershell
cd backend
# 开发（无云依赖）：BM25 稀疏路即可运行全部功能
python tools/build_retrieval_index.py --target sparse --dry-run   # 只看切片统计
python -m uvicorn app.main:app --reload                           # web /ops/rag 可视化管理

# 生产（Zilliz + 百炼 embedding 配置后）
python tools/verify_zilliz.py                                     # 集群连通性自检
python tools/build_retrieval_index.py --target dense --smoke      # 全量灌库 + 冒烟
python tools/eval_rag.py --with-dense                             # 策略评测（含云端召回）
```

故障降级矩阵：

| 故障 | 行为 |
| --- | --- |
| Zilliz 不可达 / Token 失效 | dense 路空转或降级纯稀疏，warning 入运行轨迹，检索不中断 |
| embedding 服务限流/超时 | 灌库自动指数退避重试；查询路 dense 失败降级同上 |
| 重排 API 故障 | 回退 lexical 重排，warning 入轨迹 |
| LLM 未配置 | Agent 确定性模式（不影响检索层） |
