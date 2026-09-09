# RAG 技术选型方案（含实测佐证）

> 结论基于 2026-09 真实数据库（908 车系 / 6,608 款型 / 73.9 万参数事实）与 520 题四桶黄金集的
> 实测收敛，全部指标可用 `backend/tools/eval_rag.py` 复现（`eval/*.json` 为逐轮存档）。
> 架构与运维细节见 `RAG.md`；本文回答「每个环节选了什么、为什么、数据在哪」。

## 0. 结论速览

| 环节 | 选型 | 关键佐证（详见对应章节） |
| --- | --- | --- |
| 编排 | LangGraph `StateGraph` 双流水线（查询/摄取） | 条件扇出并行召回、节点轨迹可观测、故障逐级降级（RAG.md §1-2） |
| 切分 | **款型级合并切片**为主 + 车系摘要切片 + 文档递归切分（500/64） | 对比单事实切片：Hit@5 +2.0pt、MRR +1.5pt（§2） |
| 中文分词 | **bigram**（汉字二元组） | jieba 整词 Hit@5 −1.55pt；整词+二元组 hybrid 仍不及（§2.3） |
| 召回 | **双路混合**：进程内 BM25 ∥ Zilliz 稠密（qwen3.7-text-embedding-flash，1024 维，COSINE） | dense 单路 0.643 < sparse 0.672（实体型语料）；但 semantic/recommend 桶稠密 MRR 占优 → 互补而非替代（§3） |
| 查询理解 | **确定性**实体解析 + 意图分流（无 LLM、可复现） | v9b 修正后 pipeline 首次全面 ≥ 裸稀疏（§4.1） |
| 融合 | 加权 RRF（k=60），路权重按查询类型 | hybrid 全局 Hit@5 +0.23pt、semantic 桶 MRR +68%（§4.2） |
| 实体锚定快路 | parameter / compare **只走稀疏**（跳过稠密与重排） | dense 拉低 compare 桶 0.873 vs sparse 1.0；覆盖 43% 流量，端到端延迟与 API 成本同降（§4.3） |
| 精排 | qwen3.7-text-rerank（Cross-Encoder）**仅 semantic/recommend 启用** | 终版 Hit@5 0.6763（历史最高），全面 ≥ 不重排；全量重排的 compare 桶损失被消除、耗时 −34%（§5） |
| 证据把关 | 丢弃空文本；Cross-Encoder 绝对分可配阈值（默认关） | 阈值在锚点口径中性，保留为生产可调项（§5.3） |
| 索引运维 | 幂等重建 + **流式 JSONL 向量缓存**断点续跑（2C2G 低内存安全）+ insert 瞬态重试 | 实测 Zilliz serverless 504 冷启动可自动恢复；12,078 条重建 ~25 分钟（§6） |

**最终形态一图**（查询流水线，`app/rag/pipeline.py`）：

```text
analyze（实体解析 + 意图分流）
├─ parameter / compare（43% 流量，实体锚定）
│    └─ BM25 稀疏快路 → RRF(单路) → 保持融合序 → grade
└─ semantic / recommend（语义/诉求型）
     ├─ BM25（entity_query，不掺同义扩展）
     ├─ Zilliz 稠密（search_query 含同义扩展）
     ├→ 加权 RRF 融合 → qwen3.7 Cross-Encoder 精排 → grade（可选阈值）
```

## 1. 评测基准（全部结论的可复现口径）

- **黄金集**：`gen_eval_questions.py --count 520`，从数据库分层抽样生成，anchors 即相关性判定
  （零人工标注）。四桶：recommend 246 / parameter 115 / semantic 80 / compare 79。
- **有效锚点题 451 条**：无 series/variant 锚点的题目（open_clarify 39 + 仅品牌锚 30）不参与计分
  （`eval_rag.py` 按锚点缺失跳过），指标不会被「无标准答案」的题稀释。
- **指标**：Hit@5 / Recall@5 / Precision@5 / MRR / NDCG@10（二元相关，取回深度 10）+ 分桶报告。
- **口径局限（解读结论时须知）**：
  - semantic 桶有效题仅 41 条（semantic_fuzzy），单题变动 = ±2.4pt，该桶指标看趋势不抠个位；
  - recommend 桶锚点是「满足约束的任一车系」（生成时随机指定），ID 命中是**下界**——
    预算/座位/能源类诉求本质是多对一约束满足，评测低估真实可用性；
  - 参数/对比桶锚定款型/车系名，是**实体精确匹配**口径，天然偏向词面检索。

## 2. 切分策略：款型级合并切片（已验证的最大单项收益）

### 2.1 选型：四类切片

| kind | 粒度 | 内容 | 服务的查询 |
| --- | --- | --- | --- |
| `variant_spec`（10,262 条） | **款型** | 一个在售款型的核心事实按优先级合并 1~N 片，头部逐片重复（品牌+车系+款型名+能源+指导价），任意切片独立可读 | 「X 的续航是多少」等 SKU 级参数题 |
| `series_summary`（908 条） | 车系 | 一句话画像：定位/指导价区间/核心参数/在售数/月销量 | 「X 怎么样/有什么优点」 |
| `series_intro`（908 条） | 车系 | 定位 + 能源类型 | 品牌/能源筛选类 |
| `source_document`（0 条，当前 RDS 文档正文为空） | 文档 | 递归字符切分（段落→句读→硬切，500 字/64 重叠） | 文档正文入库后启用 |

### 2.2 为什么款型级合并而非单事实切片

- 单事实切片（v0）把「一个款型 = 几十条键值行」拆成碎片，同键同值跨款重复行吃光车系配额，
  高频参数（续航/油耗）反而搜不到（实测「续航多少」检索为空）；
- 合并后**命中即对齐 SKU**（metadata 全量携带 series_id/variant_id/price_cny/status/energy_type），
  证据归属映射消失，Agent 引用无需二次解析；停售款型不进索引，证据不被过时参数污染；
- **实测**（520 题同口径）：v0 单事实 → v2 款型级合并，Hit@5 0.6519→**0.6718**（+2.0pt）、
  MRR 0.6337→**0.6488**（+1.5pt），且此后所有迭代的基线增益都建立在该切分之上（RAG.md §4.2）。

### 2.3 分词：bigram 胜出（反直觉但有数据）

| 分词 | Hit@5 | MRR | 备注 |
| --- | --- | --- | --- |
| **bigram（默认）** | **0.6718** | **0.6488** | 对词表不匹配天然鲁棒（「纯电续航」vs「CLTC纯电续航里程」） |
| jieba 整词 | 0.6563 | 0.6421 | 领域词表 + 车系专名注册仍不齐；词表维护是持续成本 |
| jieba hybrid（整词+二元组） | 0.6674 | 0.6485 | 精度收益 < 召回损失 |

车系专名（`register_tokens`）继续运行期注册进 jieba，服务同义扩展的整词匹配，不影响 BM25 口径。

## 3. 召回策略：双路混合，稠密补语义洞而非全量替代

### 3.1 单路实测（2026-09-08，款型级切片全量重建后）

| 策略 | Hit@5 | Recall@5 | P@5 | MRR | NDCG@10 | 延迟 |
| --- | --- | --- | --- | --- | --- | --- |
| sparse（BM25, bigram） | **0.6718** | **0.2636** | **0.5632** | 0.6488 | 0.5529 | ~24ms/题 |
| dense（Zilliz 向量） | 0.6430 | 0.2344 | 0.5082 | 0.6295 | 0.5127 | ~857ms/题 |
| hybrid（RRF 融合） | **0.6741** | 0.2620 | 0.5596 | **0.6535** | **0.5554** | ~659ms/题（可并行） |

### 3.2 结构解读（为什么本语料稀疏强、稠密不可弃）

- **本语料查询高度实体锚定**（车系名/款型名/参数键/价格数字），bigram + 专名注册的词面精确
  匹配是第一生产力 → dense 单路全局落后 2.9pt；
- **但分桶暴露稀疏的语义洞**：semantic 桶 MRR 稠密 0.0811 vs 稀疏 0.0488（+68%）——
  无实体诉求题（「帮我推荐一台油电混动的SUV，中型SUV {定位描述}」）靠 embedding 语义匹配
  定位到目标车系的摘要/介绍切片，词面匹配几乎失效；
- **dense 的实体混淆必须隔离**：compare 桶 dense 单路 Hit@5 0.8734 vs sparse 1.0000——
  向量空间分不清「两个被比款型」的实体边界，语义相近的第三车系切片会挤进候选；
  parameter 桶同理（0.9913 vs 1.0000）。
- **结论**：稠密路的价值在 semantic/recommend（兜底无实体表述），必须与稀疏并行并用
  RRF 融合，而不是二选一；实体锚定查询走稀疏快路规避稠密噪声（§4.3）。

### 3.3 稠密实现要点

- embedding：阿里百炼 `qwen3.7-text-embedding-flash`（1024 维，OpenAI 兼容协议）；
- 向量库：Zilliz Cloud Serverless（杭州），COSINE / autoindex，meta 携带全量过滤字段
  （series_id/variant_id/energy_types/status/price_cny/body_type），检索期过滤下推；
- 查询侧每 semantic/recommend 题 1 次 embedding 调用；灌库侧 12,078 条一次性
  （本地缓存后重复重建零成本）。

## 4. 查询理解与融合

### 4.1 确定性 analyze（不用 LLM 做查询理解）

车系名索引解析（归一化子串、最长优先）→ 实体增强查询（别名→规范名）+ 元数据过滤。
选型理由：可复现、零延迟/成本、错误可定位；LLM 理解仅在 Agent 会话层使用，检索层保持确定性。
迭代教训（v7→v9b）：同义扩展污染稀疏查询（Hit@5 −2.9pt）、对比类误加单车系过滤
（compare 桶 1.0→0.835）都是 analyze 层问题，靠分桶评测定位后修正，修正后 pipeline
首次全面 ≥ 裸稀疏。

### 4.2 加权 RRF 融合

- RRF（k=60）：两路分数量纲不可比（BM25 无上界 vs COSINE ∈[0,1]），只用名次是标准解；
- 路权重按 query_type：semantic (0.4, 0.6) 偏稠密、其余偏稀疏；
- 去重两级：chunk_id（跨后端共识加权）+ 文本兜底。

### 4.3 实体锚定快路（本轮新增，实测收敛）

| 路由 | 依据 | 实测 |
| --- | --- | --- |
| parameter → 稀疏快路 | 键值模板强区分，稠密候选只会稀释 | 既有优化④ |
| **compare → 稀疏快路**（本轮） | dense 拉低 compare 桶（0.873 vs 1.0）；融合进来的语义近邻是纯噪声 | pipeline MRR 0.6650→0.6674、NDCG@10 0.6203→0.6229、耗时 −45s，Hit@5 持平 |
| semantic / recommend → 双路 | 语义洞只能稠密补 | §3.2 |

快路覆盖 43% 有效流量（parameter 115 + compare 79 / 451），同时省掉这些查询的
embedding 调用与云端检索延迟。

## 5. 精排：Cross-Encoder 条件启用（收益集中，噪声隔离）

### 5.1 三轮对照数据（同一 520 题基准）

| 轮次 | 配置 | pipeline Hit@5 | MRR | NDCG@10 | 结论 |
| --- | --- | --- | --- | --- | --- |
| v10（纯稀疏融合） | qwen3-rerank 全量 | 0.6696 | 0.6552 | 0.6041 | 比不重排（0.6718/0.6554/0.6082）**负收益** |
| 本轮 A（compare 走稠密） | qwen3.7 rerank 全量 vs 关 | 0.6741 vs 0.6652 | 0.6650 vs 0.6520 | 0.6203 vs 0.6111 | 稠密入链后**翻正**：+0.89pt Hit |
| 本轮 B（compare 稀疏快路） | qwen3.7 rerank 全量 vs 关 | 0.6741 vs 0.6718 | 0.6674 vs 0.6587 | 0.6229 vs 0.6078 | 仍为正但收窄 |

### 5.2 现象与机制

- 纯稀疏时代重排负收益：候选已实体精确，CE 只重排不改判；
- 稠密入链后翻正：稠密带来「语义近但实体错」的候选，CE 的绝对相关分恰好过滤这种噪声
  （实测区分度 0.981 vs 0.0098，v5b）；
- **但收益集中于 recommend 桶**（开 0.4907/0.4867 vs 关 0.4722/0.4691）；实体锚定桶上
  CE 中性偏负且白付 ~1s/题（compare Hit@5 关 0.9873 vs 开 0.9747；parameter NDCG 关 0.9904 vs 开 0.9880）。

### 5.3 最终选型：按查询类型条件重排

```python
if query_type in ("parameter", "compare"):
    PassThroughReranker()        # 保持融合序：BM25 融合序已近最优
else:
    get_reranker()               # qwen3.7-text-rerank（协议自适应，失败回退融合序）
```

- 收益：recommend 桶的重排增益全保留（0.4907/0.4867，no-rerank 仅 0.4722/0.4691）；
  compare 桶恢复稀疏精度（Hit@5 0.9873 vs 全量重排 0.9747）；43% 流量端到端少一次
  Cross-Encoder API 调用（~1s/题）；
- grade 阈值（`RETRIEVAL_RELEVANCE_THRESHOLD`）仅对 CE 绝对分生效，默认 0（关）——
  锚点口径下阈值过滤实测中性，保留为生产期「证据纯度 vs 召回」的可调旋钮；
- **终版验证**（`eval/eval-pipeline-final.json`）：Hit@5 **0.6763**（历史最高）、MRR 0.6656、
  NDCG@10 0.6138、耗时 418.9s——相对**全量重排**：Hit@5 +0.22pt、MRR 持平（−0.18pt）、
  NDCG@10 −0.91pt（全部来自 compare 桶 n=79 的深度排序噪声，2 题以内波动）、耗时 **−34%**、
  CE 调用 −43%；相对**完全不重排**：Hit@5/MRR/NDCG@10 全指标占优（+0.45/+0.69/+0.60pt）。
  条件重排为生产默认。

## 6. 索引与运维选型

| 决策 | 内容 | 佐证 |
| --- | --- | --- |
| 稀疏进程内、稠密云端 | BM25 每请求重建零依赖；稠密只在 Zilliz | 原则 7（本地无云依赖可运行）；dense 故障自动降级纯稀疏 |
| 幂等全量重建 | `drop+create` 集合，索引构建 = 全量重建语义 | schema 变更零迁移成本（本次款型级切片重建直接切换） |
| **embedding 流式向量缓存** | JSONL 追加文件（每行 `{"k","v"}`），随批滚动灌库、内存只有单批向量；中断后重扫文件建偏移索引，已嵌入切片免重嵌 | 2C2G 部署实测教训：全量向量驻留吃穿内存；本轮中断续跑实测零重复调用 |
| **insert 瞬态重试** | 100 行/批、120s 超时、504/408/超时线性退避重试 5 次 | 实测 serverless 冷启动首批 insert 必 504，退避后全部成功 |
| 故障降级矩阵 | dense 失败→纯稀疏；重排失败→融合序；LLM 未配置→确定性 Agent | 检索永不因云依赖中断（RAG.md §7） |

## 7. 最终推荐配置（backend/.env）

```ini
RETRIEVAL_BACKEND=milvus                  # 双路召回（未配置 Zilliz 时自动纯稀疏，功能不缺）
MILVUS_URI=...  MILVUS_TOKEN=...  MILVUS_COLLECTION=car_docs  MILVUS_DIM=1024
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode
EMBEDDING_MODEL=qwen3.7-text-embedding-flash  EMBEDDING_DIMENSIONS=1024
RETRIEVAL_EMBED_CACHE=./.embed_cache.json # 灌库断点续跑（已 gitignore）
RETRIEVAL_TOKENIZER=bigram
RETRIEVAL_RECALL_MULTIPLIER=6             # 每路召回 30（20~100 截断）
RETRIEVAL_RRF_K=60
RERANK_PROVIDER=api                       # 仅 semantic/recommend 生效（pipeline 内条件路由）
RERANK_BASE_URL=https://dashscope.aliyuncs.com/api/v1
RERANK_MODEL=qwen3.7-text-rerank
RETRIEVAL_RELEVANCE_THRESHOLD=0           # 生产可调：证据纯度 vs 召回
RETRIEVAL_CHUNK_SIZE=500  RETRIEVAL_CHUNK_OVERLAP=64   # source_document 递归切分
RETRIEVAL_FACTS_PER_VARIANT=30            # 款型切片事实配额（×5 过采样去重）
```

### 延迟与成本预算（实测口径，eval 为串行召回、生产双路并行后更低）

| 查询类型 | 占比（有效锚点） | 路径 | 端到端延迟 |
| --- | --- | --- | --- |
| parameter / compare | 43% | 稀疏快路（无 embedding/无重排 API） | ~0.05-0.1s（进程内） |
| semantic / recommend | 57% | 双路 + RRF + CE 重排 | ~1.5-1.9s（串行实测；并行后 ≈ max(路) + 重排） |
| 全体均摊 | - | 终版 pipeline 418.9s / 451 题 | **~0.93s/题**（对照：全量重排 1.51s/题） |
| 灌库（一次性/月度） | - | 12,078 条 embedding + upsert | ~25 min（本地缓存后续跑秒级） |

## 8. 局限与后续迭代（按优先级）

1. **评测口径升级**：recommend/semantic 桶改「约束满足度」判定（返回车系满足
   预算/座位/能源/body 即计分），消除随机锚点带来的下界失真；open_clarify 无锚题
   引入 LLM-judge 或剔除出主表；
2. **semantic 桶重排对照扩样**：n=41 下 no-rerank 两次反超 rerank（0.1707/0.1951 vs
   0.1463），但单题波动 ±2.4pt，扩样后若仍负，semantic 也可关重排；
3. **HyDE 待启用**：`RETRIEVAL_HYDE` 已实现（semantic 查询 LLM 生成假设证据做稠密召回），
   待语义桶扩样评测后决定是否默认开启；
4. **source_document 切片为空**：官方文档正文入库后，递归切分（500/64）与重叠收益需复测
   （当前 sparse 与 sparse-nooverlap 同分为口径空转）；
5. **真实查询日志回流**：黄金集是数据库抽样，上线后用真实 query 分布复核意图分流比例与
   分桶权重（`RETRIEVAL_RRF_WEIGHT_*` 可按线上分布再调）。

## 附：本轮评测产物索引

| 文件 | 内容 |
| --- | --- |
| `backend/eval/eval-dense-full.json/.md` | 款型级稠密索引重建后全策略（sparse/dense/hybrid/pipeline±重排） |
| `backend/eval/eval-pipeline-compare-sparse.json/.md` | compare 稀疏快路对照 |
| `backend/eval/eval-pipeline-final.json/.md` | 条件重排终版 |
| `backend/eval/ab-v*.json/.md`、`baseline-v0.json` | 纯稀疏时代逐轮 A/B 存档（RAG.md §4.2） |
