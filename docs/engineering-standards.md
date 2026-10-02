# 工程规范

> 面向人与 AI agent 共用的规范性文档。每条规则用统一格式：**规则 → 判定方式 → 现状基线**。
> 「现状基线」中的数字均为 **2026-10-02 实测**（分支 `review`，HEAD `bd5dc5d`），用于判断存量欠账，**不是达标线**。
>
> 职责边界：本文件管**代码与架构**。协作流程（并行会话纪律、提交信息、Instincts）见 `AGENTS.md`，本文件不重复。
> 阶段计划与改动清单见 `docs/refactoring-roadmap.md`。产品能力见 `README.md`，RAG 设计见 `RAG.md`。

---

## 1. 适用范围与术语

### 1.1 效力

本文件是**规范性**的：条款用「必须 / 禁止 / 应当」表述，每条都给出可判定的检查方式。
与之相对，`AGENTS.md` 的 Instincts 是**启发式**的——它们天然不可机器化，靠人记住，靠事故强化。两者都要读。

### 1.2 术语

| 术语 | 定义 |
| --- | --- |
| **确定性内核** | 结论只由数据库事实与纯计算产生、不经过 LLM 自由生成的代码路径 |
| **降级（degrade）** | 上游不可用时回退到更弱但**诚实**的输出（显式标注「官方资料未披露」），而非静默伪造 |
| **编排层** | `app/agent/` 与 `app/retrieval/`：调用域模块并组织流程，不直接写业务规则 |
| **域层** | `app/vehicles`、`app/comparison`、`app/recommendation`、`app/rag` 等：各自拥有业务规则 |
| **公共层** | `app/common/`：配置、数据库、模型、错误、限流等跨域基础设施 |
| **零回归** | 见 §9.1：改动后关键指标逐位一致，不是「差不多」 |

---

## 2. 架构分层与依赖方向

### 2.1 允许的依赖方向

```text
        装配层  main.py
            ↓
        编排层  agent/ · retrieval/ · rag/
            ↓
         域层  vehicles/ · comparison/ · recommendation/ · sales/
            ↓
        公共层  common/ · catalog/ · variants/ · enums
```

### 2.2 规则

- **R2.1** `app/common/` **禁止** import 任何域层模块。它是所有域的地基，一旦反向依赖即形成环。
- **R2.2** `app/*` **禁止** import `tools.*`。当前实测为零违反——保持零。
- **R2.3** 域层之间**禁止**直接 import 对方的实现模块；必须经 `app/common/` 或显式的公共契约模块。
- **R2.4** 编排层**禁止**在自身内写业务规则（评分、口径、优先级）。规则必须在域层。

**判定方式**：**当前尚未建成**独立检查脚本——这一维度由 `tools/quality_metrics.py` 的
fan-out 指标与本文件的人工评审共同覆盖。待补的检查应扫描 `app/*` 下的 `import app.*`，
命中 R2.1/R2.2/R2.3 即失败（落在根 `tools/` 目录，与 quality_metrics 同级；
**尚未创建，故此处不写出路径以免被悬空引用门禁拦下**）。

**现状基线（2026-10-02）**：**3 处真实 import 环**
- `app.rag.ingest` ↔ `app.rag.service`
- `app.rag.pipeline` ↔ `app.rag.service`
- `app.retrieval.backends` ↔ `app.retrieval.zilliz`

**1 处分层倒置**：`backend/app/recommendation/router.py:15-16` 反向 import `app.agent.schemas` 与
`app.agent.tools`。同时 `backend/app/agent/engine.py:88` 依赖 `app.comparison.analysis`——两个域互指。
**处置**：R3 的拆分（`docs/refactoring-roadmap.md` P3）会顺带解耦；本轮不阻塞。

---

## 3. `app/` 与 `tools/` 边界

### 3.1 规则

- **R3.1** `tools/` **只能** import `app.*`，反向永远禁止。
- **R3.2** **可复用判定**：任何会被 API 路由、或被第二个 `tools/` 脚本调用的逻辑，**必须**下沉到 `app/`。判据是「调用方数量 ≥ 2」，不是「代码长」。
- **R3.3** 一次性数据修复脚本（`dedupe_conflicting_facts.py`、`rename_conflicting_facts.py`、`fix_energy_labels.py`）**不适用 R3.2**——它们按设计需要携带一份与库内规则**独立**的副本（见 §3.3 的说明），且带一致性夹具保护。这类脚本在生产数据清理完毕后应进入退役倒计时。
- **R3.4** `tools/` 脚本的启动样板（argparse、环境引导、`sys.path` 插入、`load_dotenv`）必须收敛到计划新增的 `backend/tools/_bootstrap.py`，**禁止**逐字复制。

**判定方式**：R3.1 由架构检查覆盖；R3.4 由重复率检查覆盖（见 §4.4）。

### 3.2 为什么 R3.2 是「调用方数量」而不是「行数」

审计中曾误判 `backend/tools/fetch_autohome_sku.py`（508 行）是 `app/sources/autohome_sku.py`（686 行）的分叉副本。
实测：它 **import** 了库的解析函数，1204 行 diff 是增量编排（断点续传、CLI、品牌注册表）。**无需合并。**

### 3.3 刻意保留的重复（不要「顺手清理」）

| 重复 | 为什么保留 |
| --- | --- |
| `dedupe_duplicate_keys()`（`app/sources/autohome_sku.py` 与 `tools/dedupe_conflicting_facts.py` 各一份） | 工具必须能修复**尚未应用修复**的部署，因此必须内联旧规则；`backend/tests/test_dedupe_conflicting_facts.py` 有跨副本一致性夹具 |
| `_headers()` / `model()` 等 3 行访问器 | 抽象收益低于成本，合并反而增加耦合面 |
| `SessionStore` / `RedisSessionStore` 的同名方法 | 二者 `clear` 语义**已经微妙分歧**，此时合并会把分歧固化成契约。须先统一语义（roadmap P4.3）再谈去重 |

---

## 4. 代码质量指标与上限

> **指标是信号，不是目标。** 为降数字而拆出的无意义中间层，比高数字本身更糟。
> 每条阈值都配「存量递减冻结」：**本 PR 不得使任何超标指标的数值上升**，但不要求一次性清零存量。

### 4.1 复杂度必须成对判定

**R4.1** 任何复杂度判断**必须同时给出认知复杂度与圈复杂度**（McCabe），不得只看其一。

**为什么**：两者排序不同，单看会排错优先级。实测对照：

| 函数 | 认知复杂度 | 圈复杂度 | 嵌套深度 | 正确判读 |
| --- | --- | --- | --- | --- |
| `backend/app/comparison/analysis.py:177` `analyze_comparison` | 261（第 2） | **148（第 1）** | 6 | **两项都最高** → 真正的结构性复杂，必须重构 |
| `backend/tools/gen_eval_questions.py:191` `_build_questions` | **270（第 1）** | 56（第 6） | **18（全仓最深）** | **认知第 1、圈只第 6** → 单点深嵌套所致，**拆平嵌套即消失**，不需要大重构 |

**判读指引（两值背离时）**：
- 认知 ≫ 圈 → 深嵌套型（大量 `elif` / 布尔短路串联）。**拆嵌套**，不要拆职责。
- 圈 ≫ 认知 → 分支平铺型（大量独立 `if` 早返回）。**改表驱动 / 策略映射**。
- 两值都高 → 职责混杂，**必须拆职责**。

### 4.2 八维指标与阈值

| # | 指标 | 阈值 | 现状基线（2026-10-02） |
| --- | --- | --- | --- |
| 1 | 认知复杂度 | 新增 ≤ 15；≥ 26 禁止合入 | 618 个函数中 85 个 > 15、49 个 ≥ 26 |
| 2 | 圈复杂度（McCabe） | 新增 ≤ 10；> 20 须在 PR 说明 | 中位数 4、最大 148、43 个 > 20 |
| 3 | 函数长度 | ≤ 80 行 | 18 个 > 80；最长 `backend/app/agent/engine.py:761` `respond` 347 行 |
| 4 | 文件规模 | ≤ 600 行 | 7 个 > 600；最大 `backend/app/agent/engine.py` 1968 行 |
| 5 | 嵌套深度 | ≤ 4 层 | 最大 18 层 |
| 6 | 长参数列表 | ≤ 5 个（**见 §4.3 排除项**） | 31 个 ≥ 6（含假阳性） |
| 7 | 重复率 | < 5%（**见 §4.4 归类要求**） | 14.3%，其中 224/289 个重复块跨文件 |
| 8 | 耦合度 fan-out | ≤ 10 | `backend/app/main.py` 17、`backend/app/agent/engine.py` 14 |

补充：**死代码目标为 0**。当前疑似 1 处；`TODO`/`FIXME`/`HACK`/`XXX` 全仓仅 2 处——**这是本仓的亮点，规范予以肯定而非批判**。

**判定方式**：1/2/6 由 `ruff` 的 `C901`、`PLR0912`、`PLR0915`、`ARG001` 产出报告（**只报不 fail 起步**）；
3/4/5/7/8 由 `tools/quality_metrics.py` 产出报告并归档为 CI artifact。**报告即看板**——看板存在即代表没忽略。

### 4.3 长参数列表的排除项

**R4.2** FastAPI 路由处理器由 `Query()`/`Path()`/`Depends()` 生成的参数**不计入**长参数列表。

实测假阳性：`backend/app/sales/router.py:30` `home()`（11 个）、`backend/app/vehicles/router.py:41` `vehicle_list()`（10 个）——框架强制，不是 smell。
真正超标的是 `backend/app/sources/fetcher.py:111` `record_snapshot()`（11）、`backend/app/sources/importer.py:169` `_resolve()`（9）。

### 4.4 重复率必须按模式归类

**R4.3** 重复率检查**必须**按模式归类输出，不接受只报一个总百分比。
14.3% 的总数里，真正值得修的只有两类：

| 模式 | 副本数 | 处置 |
| --- | --- | --- |
| `tools/` CLI 启动样板 | **8 份** | 计划抽 `tools/_bootstrap.py`（roadmap P6.1） |
| 「最新销量」口径查询 | 3 份 | 收口（roadmap P6.2，与 P5.3 合流） |
| 3 行 HTTP 头访问器 | 3 份 | **不修**（见 §3.3） |

把 3 行访问器计入重复率会淹没真正值得修的项。

### 4.5 坏味道与缺陷的优先级分离

**R4.4** 缺陷与坏味道**必须**分开排期，不得混为一谈。

| | 缺陷 | 坏味道 |
| --- | --- | --- |
| 判据 | **改变行为**（恒 500、退出码谎报、状态永久锁存） | 指标越界但**行为正确** |
| 优先级 | 高 | **全表最低** |
| 阻塞发布 | 是 | **否** |
| 触发 | 发现即修 | 仅在**已触及该文件**时顺带降一级 |

**R4.5** 坏味道治理**不排期、不阻塞、不与功能竞争**——但**必须持续可见**（§4.2 的 CI 报告看板）。
**「最低优先级」与「不能忽略」的矛盾，靠可见性化解，而不是靠排期。**

---

## 5. 错误处理与可观测性

- **R5.1** **禁止** `except Exception: pass`。捕获宽泛异常**必须**同时 `logging` 并注释说明**为何可降级**。
- **R5.2** **降级必须可观测**。任何回退到确定性模板的路径**必须**留日志——否则 LLM 故障与正常回答在生产上不可区分，用户看到「答得很差」而我们看不到任何信号。
- **R5.3** 用户可见的失败**必须**显式呈现。系统设计原则第 1 条是「事实只来自数据库」；对应的反面是「失败时明说」，而不是静默给一个看似正常的答案。
- **R5.4** 后台任务**必须**取回异常。`create_task` 的异常若从不 `task.exception()`，只会在 GC 时打印一行 asyncio 警告，落在应用日志之外——**静默死亡**。

**现状基线（2026-10-02）**：`app/` 下 67 个 except 块，其中 22 个宽泛 `Exception`、9 个 `except ...: pass`。
`backend/app/agent/engine.py` 有 **6 处** `except Exception: pass` 无任何遥测（1745、1755、1795、1854 等），
使得 LLM 故障与正常回答在生产上**不可区分**。
`backend/app/agent/engine.py:677` 的 shadow 旁路任务只 `discard` 不取异常——与已修复的 `_shadow_route` 事故同类。

---

## 6. 并发与单例语义

- **R6.1** **禁止**把「首次探测结果」永久缓存为模块级单例。Redis 不可达时回退进程内实现是正确设计，但**探测必须可重试**——首次失败就永久锁死会造成多 worker 脑裂。
- **R6.2** `async def` 内**禁止**裸调同步 `Session`。必须 `run_in_threadpool` 包裹。
- **R6.3** 进程内单例**必须**提供 `reset_*()` 供测试隔离，否则测试间状态泄漏。
- **R6.4** 进程内实现与 Redis 实现**接口必须完全一致**——这是「未配置 Redis 也能跑通」的设计前提。

**现状基线（2026-10-02）**：
- 11 个文件使用模块级 `global`。四个 store getter（`backend/app/agent/session.py:107`、`backend/app/auth/security.py:104`、`:117`、`:130`）**首次探测即永久锁存**。
  `backend/app/common/redis_client.py:24-25` 的注释自陈了「调用方回退进程内存储造成多 worker 脑裂」——而这些 getter 正是未解锁的调用方。
- `backend/app/agent/engine.py` 中 **14 处**在 `async def` 内裸调同步 `Session`；
  而同文件 `_variant_diff_reply` 已正确包裹 `run_in_threadpool`——**规则被学会但应用不一致**。
- `reset_oss()` / `reset_redis()` 存在（R6.3 的正面例子），但 `get_agent_engine()` 没有。

---

## 7. 数据访问规范

- **R7.1** **N+1 禁令**：循环内禁止每轮单独查询。必须批量装载后内存关联。
- **R7.2** **无界查询禁令**：必须下推 `LIMIT` / 聚合到 SQL。禁止「全表载入后在 Python 里过滤分页」。
- **R7.3** **事务原子性**：导入类操作**必须**在顶层单点 `commit`/`rollback`。把 commit 下沉到子函数会破坏整体原子性。
- **R7.4** **双栈语义一致**：SQLite 与 PostgreSQL 的行为差异（外键、并发写、锁）必须显式对齐，不能让缺陷只在生产暴露。

**现状基线（2026-10-02）**：
- N+1：`backend/app/agent/series_qa.py:495-506` 按款型逐个取价（8 款型 = 8~14 次往返）。
- 无界查询：`backend/app/agent/tools.py:345` 全量物化匹配目录后评分；`backend/app/vehicles/router.py:41` 全表载入后 Python 过滤。

**正面样板（须保持）**：
- `backend/app/sources/importer.py:382-386` — 顶层单点 rollback/commit（R7.3 的标准实现）。
- `backend/app/common/database.py:33-52` — SQLite PRAGMA 显式对齐 PG 语义，注释写清「为什么」（R7.4 的标准实现）。

---

## 8. 外部调用与合规

- **R8.1** **User-Agent 必须诚实标识**。禁止伪装成浏览器或移动端。README 的合规声明必须与实际请求头一致——用伪装 UA 抓取同时违反服务条款与项目自己的声明。
- **R8.2** 重试**必须**指数退避 + 抖动，且**只对瞬时错误**重试（5xx / 超时），不得对 4xx 重试。
- **R8.3** 重试**必须**实现在库层（`app/sources/fetcher.py`），禁止在多个 `tools/` 脚本里各写一份。
- **R8.4** checkpoint / 断点续传**必须**防并发写。
- **R8.5** 落库的错误**必须**截断（如 `validate_payload` 截断到 100 条），调用方按该上限设计，不可取消。

**现状基线（2026-10-02）**：
- `backend/app/sources/autohome_sku.py:25-28` 定义 `MOBILE_UA` 伪装 iPhone 并配 `Referer`，与 `backend/app/sources/fetcher.py:19` 的诚实爬虫 UA 策略**直接矛盾**。
- `backend/app/sources/autohome_sku.py:112`、`:123` 库层**零重试**；重试只在 `tools/` 层且为固定 3s、无退避无抖动、非瞬时错误也重试。
- `backend/app/sources/importer.py:166` 的 `errors[:100]` 截断是 R8.5 的正面样板，**不可移除**（调用方按它切片）。

---

## 9. 测试规范

### 9.1 零回归判据（本规范的核心增量）

**R9.1** 改动 `app/agent/`、`app/rag/`、`app/retrieval/`、`app/comparison/` 的 PR，**必须**跑检索评测
（`backend/tools/eval_rag.py`）并与基线**逐位比对**，**不只是跑 pytest**。

**为什么这是核心**：`AGENTS.md` 的验证基线只要求 pytest + tsc。而本项目的 pytest **无法发现检索质量回归**——
大量「指标掉了但测试全绿」的情况是可能的。基线数值记录在 `README.md` 的 RAG 评测章节，
由 `skills/doc_sync_check.py` 与人共同维护。**「不改变性能」这句话的兑现机制就在这里。**

**判定规则**：任一关键指标**逐位不一致即判回归**，回滚该 PR。**不接受「差异在噪声范围内」**——
这正是 README 已用「重构/迁移属于零回归改造」验证过的判据。

**R9.2** 改 `app/recommendation/` 或 `app/agent/tools.py` 的评分逻辑，必须跑 `backend/tools/eval_judge.py`，
`faithful_db` 与拒答诚实性**不得下降**。

### 9.2 其余测试规则

- **R9.3** 前端改动**必须**跑 `node --experimental-strip-types --test "lib/**/*.test.mts"`。
  ⚠️ CI 此前**从不执行**该套件（`ci.yml` 的 `web` job 只跑 install / tsc / build），已由 roadmap P0 修复。
- **R9.4** **测试必须复现生产路径**。种子/夹具缺生产实体时，断言的是与生产不同的代码路径，生产 bug 会被测试掩蔽。补种子时要问「生产库里有没有它」。*（沿用 `AGENTS.md` Instincts 1）*
- **R9.5** **测试必须对环境免疫**。`conftest` 显式固定/清除环境变量，不依赖本机 `.env` 的特定内容。*（沿用 `AGENTS.md` Instincts 3）*
- **R9.6** 改 async 任务/协程签名**必须**全仓搜调用点。旁路任务异常不传播，签名不匹配 = 静默死亡、日志零痕迹。*（沿用 `AGENTS.md` Instincts 2）*
- **R9.7** 新增缺陷修复**必须**配一个能复现该缺陷的测试。缺陷修复只改行为不加测试 = 下次重构会把它改回去。

---

## 10. 提交与文档一致性

- **R10.1** 沿用 `type(scope): 摘要`，**scope 必须与实际改动路径一致**。
- **R10.2** `docs(sync)` 类提交**禁止**修改门禁自身（`tools/pre-push-guard.py`、`skills/doc_sync_check.py`）——否则门禁可以改写评判自己的规则。改门禁**必须**用 `chore(hygiene)` 并经第二人评审。
- **R10.3** **门禁判定真值统一为 Git 索引**。「本地跑 == CI 跑」是硬要求：只遍历工作区的门禁会让未跟踪的本地文件造成本地红、CI 绿（或反之）。
- **R10.4** 易漂移的计数**必须有唯一真值源**。文档里**禁止**复制第二个副本——需要时链接过去。判定用 `skills/doc_sync_check.py`，不用人肉计数。
- **R10.5** 分支保护是**仓库设置**，不在代码树里。新增 CI job 后**必须**同步在 Settings 勾选为 required，否则它不会阻止合入。

**现状基线（2026-10-02）**：
- `tools/pre-push-guard.py` 的 `SCOPE_PATHS["docs"]` 允许改 `tools/` 与 `.githooks/` → R10.2 已被违反过（提交 `f590eb2` 同时改了 `tools/pre-push-guard.py` 与 `skills/doc_sync_check.py`，且已推上远端分支）。
- `reviewer/scan_secrets.py` 遍历**工作区**而非 Git 索引 → R10.3 未满足（`doc_sync_check.py` 已满足）。

---

## 附录 A：正面样板索引

新代码遇到类似场景时**照抄这些**，不要另起炉灶。

| 样板 | 位置 | 示范了什么 |
| --- | --- | --- |
| 语义对齐 | `backend/app/common/database.py:33-52` | 用 PRAGMA 显式对齐双栈语义，注释写清「为什么」 |
| 事务原子性 | `backend/app/sources/importer.py:382-386` | 顶层单点 commit/rollback |
| 错误截断 | `backend/app/sources/importer.py:166` | `errors[:100]` + 调用方按上限设计 |
| fail-closed 自证 | `backend/app/rag/service.py:333-360` | 水位检查的四个失败分支 + scale drift 检测 |
| 阈值施加条件 | `backend/app/rag/pipeline.py:421` | 仅当分数是绝对值时才施加阈值，lexical 重叠分永不被阈值化 |
| 模块即契约 | `backend/app/comparison/ai_summary.py:1-11` | docstring 写清口径、边界与失败回退 |
| 凭据处理 | `backend/app/auth/security.py:153-198` | CSPRNG + `secrets.compare_digest` + 尝试次数上限 |
| 注释写「为什么」 | `backend/app/agent/llm_router.py:517` | A/B 实测数据与判据同处一地 |
| 失败留痕 | `backend/app/main.py:101` | 探针如实报告状态并记日志 |

## 附录 B：CI 现状与缺口

| 项 | 状态（2026-10-02） |
| --- | --- |
| pytest 全量 | ✅ CI 跑（`backend` job） |
| `tsc --noEmit` + `next build` | ✅ CI 跑（`web` job） |
| 前端测试 | ❌ **CI 从不跑**（P0 修复） |
| `doc_sync_selftest.py` | ❌ **不在任何 CI job**（P0 修复） |
| ruff / eslint | ❌ 无配置（P0 建立） |
| 覆盖率下限 | ❌ 无（P0 建立棘轮） |
| 依赖升级扫描 | ❌ 无 |
| pre-push 门禁 | ⚠️ 仅本地 hook，**新 clone 没有**（P1 修复） |
| 门禁自写回 | ❌ `autofix` job 持写权限可改门禁（P1 修复） |
