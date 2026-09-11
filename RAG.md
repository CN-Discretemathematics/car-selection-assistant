# RAG 设计与运维（LangGraph 版）

> 本文是 `` §16 的实现级展开：架构、最终选型与量化依据、评测规范、运维入口。
> 权威约束（事实只来自数据库、密钥不落仓库、本地无云依赖可运行）。

## 1. 总览

RAG 编排基于 **LangGraph**（`StateGraph`），拆成两条流水线，代码位于 `backend/app/rag/`：

| 流水线 | 模块 | 节点 | 触发 |
| --- | --- | --- | --- |
| 查询（在线） | `pipeline.py` | analyze → recall_sparse ∥ recall_dense → fuse → rerank → grade | Agent 工具 `retrieval_search`、管理后台试运行 |
| 摄取（离线） | `ingest.py` | load → chunk → index | `tools/build_retrieval_index.py`、管理后台重建索引、开发模式数据变更自动重建 |

召回后端与向量库客户端保留在 `backend/app/retrieval/`（`backends.py` BM25、`zilliz.py` Milvus/Zilliz REST + OpenAI 兼容 embedding）。`app/rag/service.py` 是唯一门面：`search()` / `try_query()` / `run_reindex()` / `get_status()` / `recent_runs()` / `graph_spec()`。

依赖：`langgraph>=1.2` 与 `jieba>=0.42`（`requirements.txt`；开发环境经 `pip install --target vendor` vendor 化）。

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

1. **analyze（查询理解）**：用 `app/catalog/series_index.py` 的名称索引（车系名/品牌+车系名/别名/**在售款型显示名**，归一化子串匹配、最长优先去重）解析消息中的真实车系；生成实体增强查询（别名 → 规范名）与元数据过滤（单车系自动加 `series_id`）。确定性实现、不依赖 LLM。空查询短路到 END。
   - **查询意图分流**：确定性启发式输出 `query_type`（`parameter`：参数词+车系实体；`compare`：对比词；其余 `semantic`）——参数查询走稀疏快路（稠密路空转跳过，省 embedding 调用与延迟）；
   - **领域同义扩展**：`synonyms.py` 词典把口语/诉求词追加为证据词（「省油」→ 馈电油耗/综合油耗），只追加不改写；扩展词仅服务稠密路与重排兜底，稀疏路用 entity_query。
2. **recall_sparse ∥ recall_dense（并行多路召回）**：每路取 `top_k × RETRIEVAL_RECALL_MULTIPLIER`（默认 6，下限 20、上限 100）。
   - sparse：进程内 Okapi BM25（k1=1.5, b=0.75），分词 **bigram**（A/B 实测最优）+ 车系专名运行期注册；
   - dense：Zilliz Cloud REST v2 向量检索（cosine，autoindex），`RETRIEVAL_BACKEND=milvus` 时启用；未配置空转、失败降级纯稀疏（原则 7）；
   - **compare 双侧召回保障**：对比类查询对每个锚定车系各补一路 `series_id` 过滤召回并 round-robin 交错置前——保证双侧证据都在 top-5 内（此前单侧被词面近邻挤出候选池，pair-coverage 只有 0.439）；
   - **HyDE（可选）**：`RETRIEVAL_HYDE` 开启时 semantic 查询由 LLM 生成假设性证据文本（失败静默回退）。
3. **fuse（加权 RRF 融合）**：RRF（k=60），统一路权重 sparse 0.6 / dense 0.4（120 题五配置权重网格消融实测最优；分路权重表已删除）。按 `chunk_id` 去重，再按文本兜底去重。
4. **rerank（精排，条件启用）**：parameter/compare（实体锚定，BM25 融合序已近最优）保持融合序；semantic/recommend 才调用重排器（v13 实测收敛）：
   - `CrossEncoderReranker`（`RERANK_PROVIDER=api`）：阿里百炼 qwen3.7-text-rerank，协议自适应；密钥回退 embedding 同账号；
   - `LexicalReranker`（`lexical`）：词元重叠加权，零外部依赖；
   - `PassThroughReranker`（空）：保持融合序；重排 API 失败自动回退。
5. **grade（证据把关）**：丢弃空文本证据；Cross-Encoder 绝对相关分应用阈值过滤。lexical 相对名次分不做阈值化。
   - **约束下推（未点名车系 + 硬约束）**：analyze 从文本解析预算/能源/车身/座位（`_parse_constraints`），满足约束的证据优先（稳定重排）——实测 valid-precision 0.5517 → 0.6992。
   - compare 双侧均衡/补召回两个实现实测均未提升 pair-coverage（详见 §4.3），已回退，机制保留待解析修正后重启。

每次运行记录阶段轨迹到进程内环形缓冲（`RAG_RUN_LOG_SIZE`，默认 50），供管理后台「运行轨迹」可视化。

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

- **load**：SQL 装载原料——活跃车系、按款型分层取样的在售 SKU 事实、车系画像聚合、款型指导价。
  - **去重与配额全部下推 SQL 窗口函数**（双层 `row_number()`）——取代旧的「×5 过采样拉进内存 + Python 去重」；线上实测峰值内存 976MB → **286MB**、load+chunk 数小时 → **5.1s**；
  - 事实行只取 6 个标量列，款型/车系/品牌元数据单独一条查询（identity map 去重）；
  - 车系画像原料只取核心参数键并走流式游标（`yield_per`）；
  - **load / chunk 读完即结束只读事务**（`expunge_all + rollback`）：embedding + 上传阶段不再持有事务。
- **chunk**：构造四类切片（metadata 符合 §16.3）：

  | kind | chunk_id | 内容 |
  | --- | --- | --- |
  | `series_intro` | `series-{id}` | 品牌+车系+定位+能源类型 |
  | `series_summary` | `summary-{id}` | 车系级一句话画像：定位/指导价/核心参数/在售数/月销量 |
  | `variant_spec` | `variant-{id}#{i}` | 款型级合并切片：核心事实按优先级合入 1~N 片（头部独立可读）；metadata 全量携带 series/variant/energy/price/status；停售不进索引 |
  | `source_document` | `doc-{id}#{i}` | 来源文档正文递归切分（500/64，重叠防割裂） |

- **index**：写入目标后端（dense 走批量 embedding + Zilliz upsert，幂等重建集合，限流自动退避）。
  - **稠密集合 schema 必须用 Milvus v2 REST 完整格式**（`schema.fields` + `indexParams`）声明 `id/text(VarChar 8192)/vector/meta(JSON)`、`enableDynamicField=false`——Zilliz Cloud 对扁平 `fields` 写法**静默忽略**（2026-09-10 探针实测确认）；
  - 声明式 JSON 字段下 `/entities/search` 的 `meta` 是 JSON 字符串，检索侧 `_as_meta_dict` 容错解析；
  - `_ensure_collection` 先删后建：判断数据新鲜度以 `.tmp/dense-build-meta.json` 水位为准（`/ops/rag` 对照 `db_counts.latest_sales_month` 给出 `stale` 判定），不要用 Zilliz 控制台「创建时间」；
  - **向量缓存**：embedding 流式 JSONL 追加（`RETRIEVAL_EMBED_CACHE`，随 compose 卷持久化）——缓存命中后全量 drop+create+insert 约 **2 分钟**。

索引新鲜度：开发（inmemory）自动重建；生产（milvus）稀疏进程首用构建、稠密由销量导入驱动（flag 门控 cron 02:30 自动增量重建；向量缓存命中后全量约 2 分钟）。

## 4. 最终选型与量化指标

### 4.1 生产配置（全部经同题库 A/B/消融实测收敛）

| 层 | 最终选型 | 关键参数 | 实测依据 |
| --- | --- | --- | --- |
| 切分 | 款型级合并切片（仅索引在售） | CHUNK_SIZE 500 / overlap 64 / 每款型 30 条事实 | Hit@5 +2.0pt（0.6519 → 0.6718） |
| 稀疏召回 | 进程内 BM25 | bigram 分词 + 车系专名注册 | jieba/hybrid 均劣于 bigram |
| 稠密召回 | Zilliz 声明式集合 | qwen3.7-text-embedding-flash · 1024 维 · cosine | semantic 桶 MRR +68%，dense 单路仍低于 sparse 2.9pt |
| 融合 | 加权 RRF | k=60，sparse 0.6 / dense 0.4 | 等权 0.6948 / 纯稀疏 0.6898 / 稠密偏重有害 |
| 重排 | qwen3.7-text-rerank（条件启用） | 仅 semantic/recommend 查询调用 | v13 全面最优：Hit@5 0.6763 历史最高；CE 调用 −43% |
| 意图分流 | 确定性启发式（无 LLM） | parameter/compare 走稀疏快路 | compare 桶 dense 单路 0.873 < sparse 1.0 |
| 召回倍数 | 6 | 8 无增益 | 消融确认 |
| CHUNK_SIZE | 500 / overlap 64 | 400 → 0.6630、600 → 0.6585 | 消融确认 |
| HyDE | 关闭 | 消融无增益 | 消融确认 |
| 约束下推 | 未点名车系按满足度重排 | valid-precision 0.5517 → 0.6992 | v4 实测 |
| 拒答诚实性 | 维度级 + 按键级「官方资料未披露」 | 拒答 60/60 | v3 不可回答题实测 |

### 4.2 检索量化指标

**旧口径（451 题锚定判定 · 真云 dense）**——历史回归基准：

| 策略 | Hit@5 | MRR | NDCG@10 | 耗时 |
| --- | --- | --- | --- | --- |
| sparse | 0.6718 | 0.6488 | 0.5529 | 18s |
| **pipeline（生产）** | **0.6763** | **0.6656** | **0.6138** | 181–245s |
| pipeline-norerank | 0.6718 | 0.6554 | 0.6082 | 67s |

**工程重构零回归证据**（2026-09-11 同题库同机复测，五项指标与四分桶逐位一致）：

| 工程指标 | 重构前 | 重构后 |
| --- | --- | --- |
| load+chunk | 数小时（OOM 0/3 成功） | **5.1s** / 286MB |
| 全量重建（12,078 条） | 11 分钟（首次） | 缓存命中 **~2 分钟** |
| 重建成功率 | 0/3（OOM×2 / RDS 超时） | 3/3 |

**评测规范 v2 口径（信息需求对齐判定）**——参数/推荐/语义/对比桶的针对性指标：

| v2 指标 | sparse | pipeline |
| --- | --- | --- |
| fact-coverage@5（参数题，115 题） | 0.7234 | **0.7660** |
| valid-hit@5（recommend+semantic，257 题） | 0.4167 | **0.7885** |
| valid-precision@5 | 0.1731 | **0.6992** |
| valid-MRR | 0.2674 | **0.7027** |
| pair-coverage@5（compare，79 题） | 0.439 | **0.7073**（v6.1 解析修复+双侧召回保障） |

> 旧口径三处失真：① semantic/recommend 单锚点判定（一题多解说成不相关）→ 约束满足度判定；
> ② Recall@5 分母 = 相关车系全部切片（均值 15 条，结构上限 0.4773）→ fact-coverage 补充；
> ③ compare 单侧在场即算命中 → pair-coverage 补充。旧口径并排保留（回归可比），
> `recall_ceiling` 随报告输出。

### 4.3 答案层（LLM-as-judge + 确定性 faithful_db）

`tools/eval_judge.py`：作答走 AgentEngine（DeepSeek），judge 走独立模型
（`JUDGE_MODEL`，默认 qwen-plus/可覆盖）——judge 与作答模型分离。
100 题分层抽样（qwen3.8-flash judge）：

| 答案层指标 | 数值 |
| --- | --- |
| 双评一致率 | **0.913**（23 对） |
| 总体 faithful（judge 对照 RAG 证据） | 0.52 |
| semantic faithful | **1.0** |
| recommend faithful | 0.80 |
| **faithful_db（确定性数字溯源）** | **0.7765**（85/100） |
| completeness（参数桶） | 0.85 |

> **度量对象说明**：参数/对比桶的 judge faithful 偏低（0.04/0.25）不是幻觉——本产品的
> 参数/对比回答由确定性模板**直接从全量 DB 事实**生成（带引用），事实来源本就不限于
> RAG top-5。judge 对照 RAG 证据实际度量的是「证据对答案的支撑率」，与检索指标交叉
> 验证一致。v6 落地的 **faithful_db**（对照锚定车系 DB 数字池的确定性判定，零 judge
> 成本）才是这类回答的正确 faithfulness 度量。拒答诚实性由 `tools/eval_agent.py`
> 承担（不可回答题 60/60 正确标注「官方资料未披露」）。

### 4.4 历史评测报告索引

| 报告 | 口径 | 要点 |
| --- | --- | --- |
| `eval/ab-v9-final.json` | 451 题 · inmemory 纯稀疏 | v9b analyze 修正收敛 |
| `eval/comprehensive-baseline.json` | 451 题 · 全策略消融收敛 | 生产配置确认为局部最优 |
| `eval/eval-pipeline-final.json` | 451 题 · 真云 dense | 生产基线 0.6763/0.6656/0.6138 |
| `eval/eval-v3a.json` | 600 题 · v2 口径首跑 | semantic vhit 1.0（口径修正） |
| `eval/eval-v3b.json` | 167 改写体 | 词面泄漏量化：fact-coverage −10pt |
| `eval/eval-v3c.json` | 80 多约束专项 | 旧口径 0.075 vs v2 valid-hit 0.65 |
| `eval/eval-v4a2.json` | 531 题 · 约束下推+compare 回退 | valid-precision 0.6992 |
| `eval/eval-judge-v6.json` | 100 题 · 答案层 judge | faithful_db 0.7765、双评一致 0.913 |

（以上均在 `backend/eval/` 本地存档，不入仓库。）

## 5. 流程管理可视化

- **web 页面**：`/ops/rag`（管理凭据入口）
  - 流程图：两条 LangGraph 流水线的节点图（条件边标注）+ mermaid 源码；
  - 运行状态：后端模式、稀疏/稠密索引（含水位自证与滞后告警）、重排器、策略参数、数据库规模；
    一键重建索引（dense 后台任务 + 进度轮询）；
  - 试运行：单条查询的阶段瀑布 + 命中证据卡片；
  - 运行轨迹：最近 50 次流水线运行记录；
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
- **LangSmith（可选）**：配置 `LANGSMITH_API_KEY` / `LANGSMITH_TRACING=true` 后自动上报 trace。

## 6. 配置参考（backend/.env）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RETRIEVAL_BACKEND` | `inmemory` | `milvus` 时启用稠密召回路 |
| `MILVUS_URI` / `MILVUS_TOKEN` / `MILVUS_COLLECTION` / `MILVUS_DIM` | - | Zilliz Cloud（生产经 KMS 注入） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_DIMENSIONS` | - | OpenAI 兼容 `/v1/embeddings` |
| `RETRIEVAL_EMBED_CACHE` | 空（关） | 稠密灌库流式 JSONL 向量缓存（断点续跑、低内存） |
| `RETRIEVAL_CHUNK_SIZE` / `RETRIEVAL_CHUNK_OVERLAP` | 500 / 64 | 递归切分参数（字符） |
| `RETRIEVAL_RECALL_MULTIPLIER` | 6 | 每路召回 = top_k × N（20~100 截断） |
| `RETRIEVAL_FACTS_PER_VARIANT` | 30 | 每款型入索引的去重后事实条数（SQL 窗口去重 + 配额截断） |
| `RETRIEVAL_MAX_CHUNKS` | 60000 | 全库切片上限 |
| `RETRIEVAL_RRF_K` | 60 | RRF 平滑常数 |
| `RERANK_PROVIDER` | 空（lexical） | `api` 启用 Cross-Encoder（仅 semantic/recommend 实际调用） |
| `RERANK_BASE_URL` / `RERANK_MODEL` / `RERANK_API_KEY` / `RERANK_TIMEOUT_SECONDS` | - | 如硅基流动 + BAAI/bge-reranker-v2-m3 |
| `RETRIEVAL_RELEVANCE_THRESHOLD` | 0（关） | >0 时仅对 Cross-Encoder 绝对分生效 |
| `RETRIEVAL_LEXICAL_BOOST` / `RETRIEVAL_LEXICAL_CAP` | 0.05 / 0.25 | lexical 重排加分步长/封顶 |
| `RAG_RUN_LOG_SIZE` | 50 | 运行轨迹环形缓冲长度 |
| `KMS_ROLE` / `KMS_SECRET_NAME` / `KMS_REGION` / `KMS_SECRET_FORMAT` / `KMS_FAIL_OPEN` | - | KMS 凭据管家注入（见 deploy/KMS_SETUP.md） |

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

生产水位核对与自动重建：

- **水位自证**：`/ops/rag` 对照 `dense.built_at / sales_month`（构建标记
  `.tmp/dense-build-meta.json`）与 `db_counts.latest_sales_month`，滞后自动 `stale`——
  不要用 Zilliz 控制台「创建时间」判断新旧（insert 不改它，只有 drop 重建才刷新）；
- **重建由销量导入驱动**：新月度入库写 `sales-changed.flag`，cron 02:30 自动增量重建
  （向量缓存命中后全量约 2 分钟）；无新月度零成本跳过；
- **容器内跑评测**（`eval/` 问题库不在镜像内，经持久卷传入）：
  ```bash
  docker exec -w /srv/carsel/backend deploy-api-1 python tools/eval_rag.py \
    --questions .tmp/questions-v3.json --with-dense --only pipeline,sparse \
    --report .tmp/eval-v4a2.json
  ```

故障降级矩阵：

| 故障 | 行为 |
| --- | --- |
| Zilliz 不可达 / Token 失效 | dense 路空转或降级纯稀疏，warning 入运行轨迹，检索不中断 |
| embedding 服务限流/超时 | 灌库自动指数退避重试；查询路 dense 失败降级同上 |
| 重排 API 故障 | 回退 lexical 重排，warning 入轨迹 |
| LLM 未配置 | Agent 确定性模式（不影响检索层） |
| KMS 凭据拉取失败 | 容器启动阻断（fail-closed）；KMS_FAIL_OPEN=1 可降级 |
