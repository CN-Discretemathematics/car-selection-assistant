# 优化路线图

> 本文是**改动清单与阶段计划**，规则本身见 `docs/engineering-standards.md`。
> 所有基线数字均为 **2026-10-02 实测**（分支 `review`，HEAD `bd5dc5d`），经 AST 静态扫描与门禁实跑得出。
> 用例数、迁移数等易漂移计数的**唯一真值源是 `README.md` + `skills/doc_sync_check.py`**，本文不复制副本。

---

## 0. 两条泳道

审计把待办分成两类，**优先级差一个数量级**，必须分开排期：

| | 缺陷泳道（P0–P5） | 坏味道泳道（P6） |
| --- | --- | --- |
| 判据 | **改变行为**——恒 500、退出码谎报、状态永久锁存 | 指标越界但**行为正确** |
| 优先级 | 高 | **全表最低** |
| 阻塞发布 | 是 | **否** |
| 触发 | 发现即修 | **仅在已触及该文件时顺带降一级** |
| 验收 | §6 回归基线逐位一致 | 指标下降，且行为判据**同样适用** |

**「最低优先级」与「不能忽略」的矛盾靠可见性化解，不靠排期**：P0 建立的 CI 报告看板持续产出指标，
看板存在即代表没忽略；无需专人排期即代表没有与功能竞争。

```text
        ┌──────── 缺陷泳道（改变行为，优先）────────┐
P0 基线 ──▶ P1 治理 ──▶ P3 拆分 ──▶ P4 修复 ──▶ P5 性能
                └──▶ P2 去重（可穿插）──────────────┘

        ┌──── 坏味道泳道（只增维护成本，最低）────┐
  P6   8 维指标看板 + 触及即降级 + 不修清单      │
        └──────────────────────────────────────┘
```

---

## 1. 现状基线（2026-10-02 实测）

| 指标 | 值 |
| --- | --- |
| `backend/app/` | 82 文件 / 12,649 行 / 494 函数 |
| `backend/tools/` | 22 脚本 / 4,459 行 |
| `backend/tests/` | 49 文件 / 455 个 test 函数 |
| 认知复杂度 > 15 | 85 个函数（≥ 26 者 49 个） |
| 圈复杂度 > 20 | 43 个函数（最大 148） |
| 函数 > 80 行 | 18 个（最长 347 行） |
| 文件 > 600 行 | 7 个（最大 1968 行） |
| 嵌套 ≥ 6 层 | 5 处（最深 18 层） |
| 重复率 | 14.3%（289 个重复块，224 个跨文件） |
| 真实 import 环 | **3 处** |
| 静态分析基础设施 | **零**（无 pyproject / ruff / mypy / eslint / 覆盖率） |
| 死代码疑似 | 1 处；`TODO`/`FIXME`/`HACK`/`XXX` 仅 2 处（本仓亮点） |

**最重的 5 个函数**：

| 认知 CC | 圈 CC | 位置 | 名称 | 判读（见规范 §4.1） |
| --- | --- | --- | --- | --- |
| 270 | 56 | `backend/tools/gen_eval_questions.py:191` | `_build_questions` | **认知第 1、圈第 6** → 单点深嵌套，拆平即消失 |
| 261 | **148** | `backend/app/comparison/analysis.py:177` | `analyze_comparison` | **两项都最高** → 真正需重构 |
| 165 | 110 | `backend/app/agent/engine.py:761` | `respond` | 次高；文件总长 1968 行 |
| 165 | 107 | `backend/app/agent/tools.py:268` | `recommendation_tool` | 同上 |
| 127 | 58 | `backend/app/sources/importer.py:87` | `validate_payload` | — |

---

## 2. P0｜工程化基线

**动机**：代码里有 **180 处** ruff/mypy 抑制注释（96 `E402`、63 `BLE001`、5 `F401`、3 `E731`、2 `SLF001`、1 `ANN001`、1 `B018`、9 `type:ignore`）+ 前端 **6 处** eslint 抑制，
却**没有任何 ruff / mypy / eslint / pyproject.toml 配置**，venv 装不上、CI 不跑。
证据强烈指向「本地跑过门禁、配置从未提交」。

**P0.1 先验证假设（不可跳过）**：实测存量违规量，再定规则集开多大。
**严禁把「推断接近清零」当作事实写进规范。** 若存量远超预期，降级为「先只开 E/F/W 基础集，BLE/SLF 渐进接入」。

**P0.2 建立配置**：

| 工具 | 配置 | 依据 |
| --- | --- | --- |
| ruff | `select = ["E","F","W","I","B","BLE","C4","SIM"]`，`line-length = 120`，`target-version = "py311"` | 现有抑制码与该集合一致 |
| ruff | 复杂度规则 `C90`（`max-complexity = 20`）、`PLR0912`、`PLR0915`、`ARG001`（`ignore-variadic-names`）——**只报不 fail** | 服务规范的 8 维指标，是 P6 看板的数据源 |
| ruff | `per-file-ignores`：`backend/tools/**` 忽略 `E402` | 96 处 E402 集中在 tools 的 `sys.path` 引导；P6.1 抽取 bootstrap 后可取消 |
| pytest | `--cov=app --cov-report=term-missing`，`fail_under` 从实测值起步并棘轮上升 | 当前零覆盖底线 |
| eslint | `next/core-web-vitals` + `react-hooks/exhaustive-deps` | 现有 6 处抑制与之匹配 |
| mypy | **暂不开** | 全动态 ORM、零注解基础，一次性开必爆噪音。推迟到 P3 之后 |

**P0.3 修 CI 缺口**：
- `pnpm test` 补进 `web` job——**前端测试此前从不在 CI 执行**。
- `skills/doc_sync_selftest.py` 补进 `gates` job——`AGENTS.md` 强制要求却无人执行。
- 新增 `lint` job 归档复杂度报告为 artifact（P6 看板）。
- **手动步骤**：分支保护是仓库设置，需在 Settings 勾选新 job 为 required（规范 R10.5）。

**P0.4** 删掉 `ci.yml` 中 `PYTHONPATH: 'vendor:.'` 的 `vendor`——该目录 0 个跟踪文件且被 gitignore，是死配置。

**P0 本轮执行结果（如实记录，勿当成「已验证」）**：

| 项 | 结果 |
| --- | --- |
| `backend/pyproject.toml` | ✅ 已建。ruff 规则集按既有 180 处抑制码反推，`mccabe.max-complexity=20` 定为**报告线非拦截线** |
| `web/eslint.config.mjs` | ✅ 配置已建；**依赖刻意未加进 package.json**（加了不更新 lockfile 会让 CI `--frozen-lockfile` 直接失败）。启用需一次真实 `pnpm add -D` |
| `web` 的 `pnpm test` | ✅ 已接进 CI——此前前端测试**从未在 CI 执行过** |
| `doc_sync_selftest.py` | ✅ 已接进 `gates` job——此前 AGENTS.md 强制要求却无人执行 |
| 删 `vendor` 死配置 | ✅ 两处 PYTHONPATH 已清理 |
| `tools/quality_metrics.py` | ✅ 已建并**实跑通过**。纯标准库 AST，8 维看板，CI 归档为 artifact。这是 P6 泳道的「可见性」技术实现 |
| **ruff 实测存量（P0.1）** | ❌ **未完成**。本机网络不可达（`pip install ruff` 连接被重置），装不上 ruff。**因此不声称「ruff 规则集已验证为绿」**——首次实跑结果待 CI 确认 |
| eslint 实跑 | ❌ 未完成，同上（且本机 pnpm 会试图重装 node_modules，已按 R10 规避） |
| 覆盖率 `fail_under` | ❌ 未设。实测基线需 `pytest-cov`，本机装不上；先只出报告，实测后再逐版本抬升 |

> 降级路径按 R11 执行：配置按证据写、不谎报已绿；能跑的部分（`quality_metrics.py`）**已实跑验证**并成为 P6 的真实看板。

---

## 3. P1｜治理修正：让门禁真的生效

| 项 | 动作 | 对应问题 |
| --- | --- | --- |
| P1.1 | `reviewer/scan_secrets.py` 改为 **Git 索引判定**（对齐 `doc_sync_check.py` 已有做法） | 现状遍历工作区 → 未跟踪的本地文件造成**本地红 CI 绿** |
| P1.2 | `.gitignore` 补 agent 可写目录 | 同上 |
| P1.3 | **CI 增加 pre-push 门禁步骤** | `core.hooksPath` 是本地配置，**新 clone 完全没有门禁**，CI 也没补位 |
| P1.4 | `SCOPE_PATHS["docs"]` 剔除 `tools/pre-push-guard.py` 与 `skills/doc_sync_check.py` | 一次 `docs(sync)` 提交可以改写评判自己的门禁（规范 R10.2） |
| P1.5 | `PUSH_GUARD_ALLOW` 环境变量 → commit body 内联 `# gate-allow: <理由>` trailer | 环境变量不留痕；CI 中 `--no-verify` 应直接失败 |
| P1.6 | `autofix` job 改「只发 PR 评论，不推分支」 | 门禁改写自己的输入，`--fix` 的正则回归可静默洗白 PR |
| P1.7 | 收窄 `doc_sync_check.py` 的 `ALLOW_WORDS` | 含「计划/待建/规划/历史/旧/曾」的行**同时豁免**悬空引用与过期模式两类检查——很宽的静默逃生口 |
| P1.8 | 迁移数规则当前空转 | 实际 8 个迁移，全仓无文档声明迁移数，规则报告绿但没守任何东西 |
| P1.9 | 强化 `.env.example` 占位符识别 | 目前只靠主机名里的 `xxx+` 侥幸通过；`你的密码` 是中文，不匹配任何模式——距失败只差一次主机名改动 |

**门禁的结构性盲区（诚实记录，不要指望它们）**：
- `scan_secrets` **从不扫 Git 历史**——提交后再删除的密钥不可见；无阿里云/飞书/火山/Anthropic 模式。
- `scan_ui_copy` 是 8 术语 + 9 短语的**封闭清单**，新造术语一律漏过；不扫 `backend/tools/**`。
- `doc_sync_check` 只守**机械可判定**的事实（计数、悬空引用、编码），**语义失真它管不了**。

---

## 4. P2｜零风险去重（只合并真重复，不动控制流）

**本轮执行结果（第一批已落地两项，均为行为等价）**

| 项 | 结果 |
| --- | --- |
| `backend/tools/_bootstrap.py` + `tools/__init__.py` | ✅ 19 个脚本的 `sys.path.insert` 样板收敛为一次 import；**96 处 `noqa: E402` 全部消失**——因为 E402 只在「import 之前出现非 import 代码」时触发，样板本身就是那条非 import 语句。**这是根因修复，不是把警告藏起来** |
| 能源泛化规则 3 份 → 1 份（`app/common/enums.py`） | ✅ 并**顺带修掉一个真实缺陷**，见下 |

**前端 fetch 去重时挖出的缺陷（已修）**：`body?.detail` 这段此前在 4 处逐字重复，
而 `body` 来自 `res.json().catch(() => null)`（类型 `any`）。后端 `detail` 并非总是
字符串——FastAPI 的 422（`RequestValidationError`）返回的是**对象数组**：

- `new Error(detailArray)` → message 变成 `"[object Object]"`；
- `setError(body?.detail)` → 数组被当 React child 渲染，抛
  `Objects are not valid as a React child` → **整页白屏**。

compare 页当前不可达（客户端已把 id 过滤为正整数），但 `rag.ts` 的管理端请求与
`auth.ts` 的表单提交**没有任何前置过滤**——后端加一条字段校验就是一次白屏。
已收敛到 `web/lib/http.ts` 并在收窄处打类型，补 6 例回归；detail 为字符串时
与原实现逐字一致。

**能源类型泛化此前有 3 份实现，其中 2 份行为并不一致**（详见 P4 台账上方）。

**来源引用块抽取时找出的既有口径差异（待产品拍板）**：「查 Source → 循环 → Citation」
此前 4 处逐字复制，但**循环上界不一致**：

| 调用点 | 循环上界 | label 后缀 |
| --- | --- | --- |
| `_comparison_analysis_reply` | `source_ids[:3]` | 配置数据 |
| **`_catalog_overview_reply`** | **`source_ids`（无上限）** | 车型数据 |
| `_brand_overview_reply` | `source_ids[:3]` | 车型数据 |
| `_tool_loop_reply` | `sorted(source_ids)[:3]` | 数据 |

即一次全库盘点可能返回**远多于 3 条**来源引用，而其他回答类型最多 3 条。

已统一为 `_source_citations(db, ids, label_suffix=..., limit=...)`，**行为逐字保持不变**，
差异以参数显式暴露。**不擅自统一口径的理由**：能源泛化那次能判定一侧是错的
（用户点名 ICE 却拿不到 ICE）；这里没有任何证据说明盘点无上限是笔误还是刻意
（如实列全来源）。已补 9 例测试钉住现状——将来若有人「顺手统一」，会先失败并
看到这是既有行为。
`agent/tools.py` 的两份**行为并不一致**：

| 用户偏好 | SQL 下推（集合展开） | Python 侧过滤（旧逐 variant 判定） | 净效果 |
| --- | --- | --- | --- |
| `["new_energy", "ICE"]` | 保留 ICE | **拒掉全部 ICE** | 明确点名要 ICE，却一个都推不出来 |
| `["fuel", "BEV"]` | 保留 BEV | **拒掉全部 BEV** | 同上 |

旧写法的前两条规则只看大类、不看显式点名的具体类型，于是「我既要新能源、也要一台 ICE」
会被**明确点名**的那一类全灭。这正是「三份实现无任何东西强制一致」的真实代价——
**SQL 已经放行的行，Python 侧又拦了下来**。

已补 `backend/tests/test_energy_prefs.py`（11 例）锁定两件事：泛化口径本身，
以及「旧写法 == 新实现」在**其余全部组合**上的穷举等价性；那两例差异被显式
钉为**已知缺陷**而非等价改写，避免以后有人把旧写法抄回来。

> **这已超出 P2「零行为变更」的 charter**。之所以仍然做：两侧不一致时，
> 必然有一侧是错的；而收敛后两侧共用同一实现，**漂移在结构上不再可能**。
> 若不接受该行为变更，回滚点即 `git revert` 本提交。

### 其余待办（本轮未做，细节见下表）

| 目标 | 现状 | 动作 |
| --- | --- | --- |
| 能源类型泛化规则 | 写 3 遍：`backend/app/agent/engine.py:240-259`、`backend/app/agent/tools.py:313-333`、`backend/app/agent/engine.py:292-299` | 提取单一实现，三处调用。**前置测试：三处当前输出一致**（今天一致，但无任何东西强制它） |
| 来源引用块 | copy-paste 6 遍 | ✅ **本轮已完成** → `engine.py` 的 `_source_citations()`（实为两个家族共 4 处同构实现；抽取时**找出一处既有口径差异**，见下） |
| `tools/` CLI 样板 | 8 份逐字重复 | ✅ **本轮已完成** → `tools/_bootstrap.py` |
| 能源类型泛化规则 | 3 份（其中 2 份行为不一致） | ✅ **本轮已完成** → `app/common/enums.py`（并修掉真实缺陷） |
| 前端 `官方资料未披露` 字面量 | 散落 4 处 | ✅ **本轮已完成** → `web/lib/labels.ts`，与后端 `MISSING_VALUE_LABEL` 对齐 |
| 前端 `priceDisplay` 空值规则 | 3 份逐字三元式 | ✅ **本轮已完成** → `api.ts` 的 `resolvePriceRangeNote`，补 5 例测试 |
| `formatPrice(null)` 与 `MISSING_VALUE_LABEL` 不一致 | 价格缺失说「暂无」，参数/在售款型缺失说「官方资料未披露」 | **刻意不改**：属产品口径决策（改 `formatPrice` 会影响所有调用点的用户可见文案）。已记入 `labels.ts` 注释，等产品拍板 |
| 前端 fetch/`ok`/`json().catch` | 4 份 | ✅ **本轮已完成** → `lib/http.ts`（**顺带修掉一处白屏**，见下） |
| `HomeFilters` / `BrowseFilters` | ~85% 重复 | **本轮刻意不做**：两个组件都带「表单状态仅 mount 时播种、从不 resync」的既有缺陷（P5.12）。在缺陷未修前合并组件等于把 bug 一起固化；应先修 resync，再合并 |

**不做**：`dedupe_duplicate_keys` 双份（有意为之 + 一致性夹具）、`_headers()`/`model()` 3 行访问器、
`SessionStore`/`RedisSessionStore` 重复方法（语义已分歧，须先由 P4.3 统一）。

---

## 5. P3｜核心拆分（纯搬迁，零回归担保）

> **铁律**：每个 PR 只做等价搬移，**不改控制流、不改 SQL 谓词、不改返回结构**。
> 任一回归判据不满足 → 立即回滚该 PR，不在其上继续叠加。

**P3.1 `engine.py`（1968 行）** — `respond()` 按代码中**已有的分节注释**拆为薄编排器：

| 新函数 | 职责 |
| --- | --- |
| `_load_merged_profile(store, session_id, message)` | 载入+合并 profile |
| `_apply_series_context(db, message, profile)` | 解析车系/品牌、维护锁定、返回上下文 |
| `_variant_diff_target(db, message, resolved, profile)` | 款型差异目标 |
| `_dispatch(db, decision, …)` | 意图分派阶梯 |
| `_clarification_outcome(profile, message)` | 澄清分支 |
| `_maybe_unlock(db, message, profile)` | 冲突解锁 |
| `_recommend_reply(db, session_id, profile)` | 推荐回复 |

**必须不变**：返回 dict 的 key 名、SQL 谓词集合、Redis 往返**只减不增**、数字白名单校验口径。

**P3.2 `tools.py::recommendation_tool`（286 行）** — 按数据流拆 5 段：
`_hard_filter_stmt` → `_load_candidate_bundle` → `_passes_python_constraints` → `_score_variant` → `pack_result`。

**P3 的真实收益是可测性而非行为**：拆完后 `_score_variant` 可对手工构造的 `CandidateBundle` 做纯单测——
而 `brand_series_count` 语义错误与 `tradeoffs` 死代码正是当前**无法被单测捕获**的缺陷类型。

**P3.3 `analysis.py::analyze_comparison`（295 行）** — 按维度处理类型拆为：冲突键收集 / flag 维度 / numeric 维度（含单位换算与不可换算分支）/ 缺口与 leader 计算。
**必须不变**：各 `note` 文本串、比较口径、单位换算后「原值 ≈ 规范值」的展示格式。

**P3.4 `importer.py`** — `validate_payload`（CC 127）与 `import_catalog`（CC 104）按实体类型拆分。
**必须不变**：`errors[:100]` 截断、**顶层单点 commit/rollback**（下沉会破坏整体原子性）。

**P3.5 消除 3 处 import 环** — `rag/service.py` 与 `ingest/pipeline` 解耦；`retrieval/backends.py` 与 `zilliz.py` 用协议/注册表打断。

---

## 6. P4｜缺陷修复台账

### Critical

| # | 位置 | 问题 | 触发条件 | 影响 |
| --- | --- | --- | --- | --- |
| **C1** | `backend/tools/fetch_autohome_sku.py:546` | `stage_sku` **无条件 `return 0`** | `--stage sku` 全部抓取失败 | 退出码报成功，cron 链路绿灯放行。**同一函数 `:532` 的注释刚记录了「抓到 0 款型不得记为完成」的事故教训，退出码却没修** |
| **C2** | `backend/app/agent/engine.py:777` | `UserProfile(**...)` 无 `try` | 存量 profile 校验失败（类型变更/脏值/半写） | 该 session 后续**每次请求恒 500 且不自愈**，仅 `/reset` 可恢复。**schema 变更 = 部署时批量 500** |

### High

| # | 位置 | 问题 |
| --- | --- | --- |
| H1 | `backend/app/agent/session.py:107` 等四个 getter | Redis 首次不可达即**永久锁存**进程内实现，多 worker 脑裂 |
| H2 | `backend/app/agent/tools.py:345`、`400-404` | 无 SQL LIMIT，全量物化；销量子查询无月份谓词 |
| H3 | `backend/app/agent/engine.py` 14 处 | `async def` 内裸调同步 `Session`，阻塞事件循环 |
| H4 | `backend/app/agent/series_qa.py:495-506` | 两条最热问答路径的 N+1 |
| H5 | `backend/app/agent/engine.py:677-679` | shadow 任务异常从未取回，静默死亡 |
| H6 | `backend/app/sources/autohome_sku.py:25-28` | `MOBILE_UA` 伪装 iPhone，与 README 合规声明矛盾 |
| H7 | `web/app/compare/page.tsx:49,63,126,160` | 4 请求串行瀑布；后端每次查看执行**两次** `analyze_comparison` |
| H8 | `web/app/ops/rag/page.tsx:296-311` | 轮询 interval 在 unmount 泄漏 |
| H9 | `web/app/ops/rag/page.tsx:245,617` | 3/4 个 tab 失败后永久「加载中…」，无重试入口 |

### Medium / Low

| 位置 | 问题 |
| --- | --- |
| `backend/app/agent/engine.py:694,712` | `llm_elapsed_ms` 只计最后一次重试，污染 p50/p95 延迟判据 |
| `backend/app/agent/engine.py:789` 等 | 单轮最多 4 次串行 `set_profile` Redis 往返 |
| `backend/app/agent/engine.py:1786-1788` | 全量 history 无 token 预算注入 prompt |
| `backend/app/agent/tools.py:458,541` | `tradeoffs` **整条链是死代码**；`backend/app/agent/engine.py:1071` 的 `break` 只跳出内层，上限失效 |
| `backend/app/agent/tools.py:397-399` | `brand_series_count` 统计**过滤后候选集**而非品牌规模，跨查询不可比 |
| `backend/app/agent/engine.py:354` | ⚠️ **本条为审计误判，已更正**：`"MPV"` 键**不是死条目**。偏好路径用小写化的 `msg_low` 匹配（`"mpv" in msg_low`），而「不要…」的 avoid 路径用的是**原始 message**、大小写敏感（`"MPV" in message`）——两个键各覆盖一种输入，删掉大写键会让「不要MPV」漏掉排除项。真正的问题是**两条路径大小写处理不一致**（一个 lower 一个原样），统一它属行为变更，需产品拍板 |
| `backend/app/agent/series_qa.py:390-400` | 多车系路径硬编码只处理 2 个车系（解析层最多返回 4 个） |
| `backend/app/agent/session.py:35-39` | `_prune()` O(n) 且每次操作都跑，活跃 session 无上限 |
| `backend/app/agent/llm_router.py:127` | 模块级 LRU 无 TTL、无 prompt 版本键，prompt 改了旧判定活到重启 |
| `backend/app/sources/autohome_sku.py:112,123` | 库层零重试；tools 层固定 3s、无退避无抖动 |
| `backend/app/retrieval/zilliz.py:302-305` | 缓存 key 在 embedder 无 `model` 属性时回落为字面量 `"embed"` |
| `backend/app/vehicles/router.py:41-115` | 全表载入后 Python 过滤分页（无 SQL 下推） |
| `web/app/components/AddToCompareButton.tsx:10` | 未订阅 `compare-changed`，与 `CompareBar.tsx:33-42` 双数据源 → 文案与点击行为相反 |
| `web/app/components/HomeFilters.tsx:15-22` | 表单状态仅 mount 时播种，从不 resync（`SearchBar.tsx:62-64` 已正确实现） |
| `web/lib/auth.ts:5-13` | `localStorage` 无保护；`favorites/page.tsx:13` 在 render 期调用，无 error boundary |
| `web/app/components/Pagination.tsx:65,79,90,99` | 裸 `<a href>`/`<form method="get">` 而非 `next/link`，每次翻页整页重载 |

**修复顺序**（先易后难、先安全后性能）：C1 → C2 → H5 → H3 → H1 → H6 → M 项 → H2/H4（性能，需评测兜底）。

**H3 的一个陷阱（本轮实测得出，务必先读）**：
「`async def` 内裸调同步 Session」不能按调用点机械替换。AST 扫出 11 处，其中多数是
`db.scalars(...)`——**它是惰性的，真正阻塞的 I/O 发生在终结操作 `.all()` / `.first()` 上**。
只把 `db.scalars(...)` 包进 `run_in_threadpool` 等于什么都没包（构造查询不碰数据库），
却多了一次线程池往返。**必须连终结操作一起搬。**

本轮已修（立即执行型，语义明确、零歧义）：
`db.get()` 3 处（`respond` 的锁定车系+品牌、`_tool_loop_reply` 的锁定车系名）、
`build_series_qa_answer()` 1 处（对齐同文件 `_variant_diff_reply` 的既有正确写法）。
`db.get` 那两处顺带消除了一个 per-id 的 N+1。

剩余待修（须逐处分析终结操作后搬，**建议与 P3 拆分同批做**——拆分后这些调用不再是
1968 行文件里的一段，而是有独立签名的纯函数，包裹边界自然清晰）：
`respond` / `_comparison_analysis_reply` / `_catalog_overview_reply` /
`_brand_overview_reply` / `_tool_loop_reply` / `_variant_diff_reply` /
`_series_qa_reply` 中的 `db.scalars(...)`。

**H1（Redis 降级锁死）本轮尝试后回滚——附实测到的架构约束，勿重复踩坑**

现象：`redis_client.get_redis()` 每 60s 自愈重试，但 `agent/session.py` 与
`auth/security.py` 的四个 getter 把**首次探测结果永久缓存**。部署瞬间 Redis
恰好不可达即锁死进程内实现直到进程结束，多 worker 永久脑裂。

**本轮已写出修复并验证其可行性，但最终回滚**，原因是一个此前没意识到的架构约束：

1. 朴素的「降级期间持续重探、恢复即升级」会让 `get_session_store()` 在运行中途
   返回**另一个实例**；
2. 而 `AgentEngine.__init__`（`backend/app/agent/engine.py:658`）是
   `self._store = store or get_session_store()`——**引擎在构造时就把 store 钉住了**；
3. 两者一分裂，路由建的会话引擎就找不到，实测 7 个 `test_tool_loop*` 用例
   报 `KeyError: <32 位 session_id>`，且**只在全量跑时出现、单跑该文件时通过**；
4. 加 autouse 的 `reset_*` 夹具会让分裂更严重（55 个失败）——因为
   `get_agent_engine()` 是**没有 reset 钩子的模块级单例**（本仓 R6.3 违规实例）。

**正确解法必须是「同一对象、换内部后端」**，而不是「换一个 store 实例」：
给 `SessionStore` 加一层可热替换的 delegate（`get_session_store()` 恒返回同一个
包装对象，Redis 恢复时只换其内部后端），这样引擎与路由永远拿到同一引用。
**这属于 P3 规模的重构，不该在 P4 里做**——先给 `get_agent_engine()` 补 reset 钩子
（R6.3），再动存储选择逻辑。

> 回滚后工作区与 HEAD 一致，全量 575 用例通过；本段是纯文档记录，无代码变更。

---

## 7. P5｜性能与前端

| 项 | 收益 / 风险 |
| --- | --- |
| H2 `recommendation_tool` 加 SQL LIMIT | 最大延迟项，**每请求无缓存**。**风险**：LIMIT 必须在硬过滤之后、评分之前，且必须复现相同行集而非截断行集——**超出纯搬迁范围，需独立产品评审** |
| H4 `series_qa` N+1 | 批量装载价格与 facts |
| `vehicles/router.py:41` 下推 SQL 过滤 | 须保持与 `/home`、详情页一致的口径 |
| `engine.py` 4 次串行 `set_profile` | 收敛为末尾单次持久化 |
| H6 `MOBILE_UA` → 诚实爬虫 UA | **合规修复**，不是风格调整 |
| 库层补重试（退避+抖动） | 消除 tools/app 两处重复实现 |
| H7 compare 请求瀑布 | 合并端点或并行；后端消除重复 `analyze_comparison` |
| H8/H9 ops/rag 轮询泄漏 + tab 永久加载 | 统一 `useAsync` 同时修两者 |
| 前端 `next/link` / `compare-changed` 订阅 / 筛选 resync / `localStorage` 保护 / 路由级 `error.tsx` | 均有同文件内的**正确实现可对齐**，属低成本高确定性 |
| `Reveal.tsx` 客户端组件下沉 | 约 40 个实例 / 40 个 observer → 单个共享 observer 或 CSS `animation-timeline: view()`。**诚实定位：中等 hydration 收益，不是整页 SSR 救赎** |

---

## 8. P6｜坏味道治理（全表最低优先级）

**定位**：**不修任何行为**，只降指标。凡是「降指标必须改行为」的项，退回 P4/P5 处理。

**执行原则**：
1. **不排期、不阻塞**——P0–P5 收尾时只处理**已触及文件**的坏味道。
2. **不与功能竞争**——评审者无权以「顺手重构」为由扩大他人 PR 范围。
3. **持续可见**——P0 的 CI 报告看板存在即代表没忽略。
4. **递减冻结优先于新增治理**——冻结指标是本 PR 的义务。

**按 ROI 排序**：

| 序 | 项 | 收益 | 成本 |
| --- | --- | --- | --- |
| P6.1 | `tools/` 8 份 CLI 样板 → 计划新增 `tools/_bootstrap.py` | 重复率 −约 3pt；消除 96 处 `E402` 根因 | 极低 |
| P6.2 | 「最新销量」口径 3 份收口 | −约 1pt；**防口径漂移** | 低 |
| P6.3 | `analyze_comparison` 嵌套拆平 | 圈复杂度 148 → ≤ 30 | 中（与 P3.3 同 PR） |
| P6.4 | `fetcher.py:111`、`importer.py:169` 真长参数列表 | 参数指标 | 低 |
| P6.5 | 前端页脚/卡片块提取 | −约 2pt | 低 |
| P6.6 | `main.py`（fan-out 17）按域拆分注册 | 耦合度 | 中 |
| P6.7 | 疑似死代码 1 处 | 死代码归零 | 极低 |
| P6.8 | `engine.py` fan-out 14 | 耦合度 | 高（P3 后自然缓解，不单独做） |

**「不修」清单**（防止为指标而抽象）：
`dedupe_duplicate_keys` 双份 · 3 行 HTTP 头访问器 · `SessionStore`/`RedisSessionStore` 重复方法 ·
FastAPI 路由的 `Query()` 参数 · 任何「拆出了无意义中间层」的降指标式重构。

---

## 9. 回归基线

**唯一真值源是 `README.md` 的评测章节 + `skills/doc_sync_check.py`**，本文不复制数字。
执行时按 `README.md` 记录的数值逐位比对：

- 检索侧：Hit@5 / MRR / NDCG@10 / fact-coverage@5 / valid-hit@5 / valid-precision@5 / valid-MRR / pair-coverage@5
- 答案侧：faithful_db、拒答诚实性、双评一致率

**判定规则**：触及 `app/agent/`、`app/rag/`、`app/retrieval/`、`app/comparison/` 的 PR，
**任一指标逐位不一致即判回归，回滚该 PR**。不接受「差异在噪声范围内」。

---

## 10. 附录：AGENTS.md 修订建议（本轮不改文件）

| 原条目 | 处置 | 理由 |
| --- | --- | --- |
| 规则 1 commit-msg 文件名仪式 | **删仪式、留不变量** | `git commit -m` 内联消息没有共享文件竞态；`<branch>-<hhmm>.txt` +「30 分钟内有更新」是不可机械化的启发式（阈值任意），保留了事故的**形状**却换了个压力下没人会执行的咒语 |
| 规则 2 精确暂存 | **保留** | 真实价值、零执行成本、无人绕过 |
| 规则 3 push 前逐提交核对 | **删除** | 被相邻门禁完全覆盖，重复仪式 |
| 规则 4 push 门禁 | **保留但改写** | 事实是「从未真正拦下过」+「新 clone 没有门禁」，须先修 P1.3 才配得上这个描述 |
| 规则 5 一分支一功能 | **保留** | 标准实践，正确 |
| 规则 6 scope 登记 | **降级为注记** | 22 条手维护词表，注释自陈在压力下持续放宽；一个每次现实变化就得放宽的规则已不再约束任何东西 |
| 验证基线 | **保留 + 补第 4 条** | 补：**改动 agent/rag/retrieval/comparison 必须跑检索评测** |
| Instincts 三条 | **完整保留** | 全文件最好的内容：每条锚定一次真实事故、措辞是可执行的检查、天然不可机器化——正因如此才可能长期存活 |
| **新增** | 架构章节 | AGENTS.md 目前 100% 流程、0% 架构。指向 `docs/engineering-standards.md` |

净效果约 −12 行，信噪比显著提升。

---

## 11. 本次 review 的能力边界

- 未执行 `next build`（会写 `.next/`）——**「CI 今日能否全绿」对 `web` job 未验证**。
- 未在真实 CI checkout 内观察 `scan_secrets`——其 CI 行为由代码路径推断，非实测。
- 分支保护状态、`autofix` job 的写权限属**仓库设置**，无法从工作区推导。
- H2（全量目录扫描）的**墙钟影响**由调用点推导，未用查询日志实测。
- 若本地无法安装 `ruff`，P0.1 的存量实测改用 AST 脚本完成，**并在 PR 描述中如实标注「未在本机验证」**，不谎报已绿。
