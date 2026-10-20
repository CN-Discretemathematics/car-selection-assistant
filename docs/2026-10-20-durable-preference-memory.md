# 跨会话偏好记忆（Phase 1）

> 分支：feat/durable-preference-memory｜日期：2026-10-20
> 依赖：[ADR-0001（画像跨会话持久化）](adr/0001-profile-persisted-across-sessions.md)
> （该 ADR 在「docs/adr-and-glossary」分支，本分支的代码按它的口径实现；
> 本分支的 PR 依赖那一份先合入，否则上面的链接指向尚不存在的文件）

## 一句话

回访用户不必重讲一遍「预算多少、要什么车、不要什么车」。

## 为什么是「记忆」而不是「把画像整份存下来」

画像里混着两类东西：**用户明说过的**（可以带走）和**模型推断的**（不能带走）。
整份持久化会把推断一起带走——而推断猜错会**静默砍掉整个候选集**，用户看到的
只是一句「没有合适的车」，永远不知道自己说过的话被改成了什么。

所以这里只持久化**封闭词表内、用户明说过的**那几条，其余一概不记。

## 三个模式闸门

`DURABLE_PREFS_MODE`（与 `AGENT_SOFT_PREF_MODE`、`AGENT_ROUTER_MODE` 同一套纪律）：

| 模式 | 回填（读） | 写入 | 日志 |
|---|---|---|---|
| `off`（默认） | 否 | 否 | 否 |
| `shadow` | 否 | 否 | 是（只记「本轮会记住什么」） |
| `on` | 是 | 是 | 只记失败/拒绝（不记「本轮会记住什么」） |

- `off` = **完全不介入**，不只是「不写库」。若只短路写入，切回 `off` 时
  之前 `on` 模式写下的记忆仍会悄悄影响推荐——那就不是「关掉了」。
  闸门关着时**连身份都不查**（`router.py` 的 `send_message` 里 `is_enabled()`
  为假就不查 `token_hash`），每次请求不多一次 SELECT。
- `shadow` = **不回填**：回填会改变推荐结果，那就已经不是「观察」了。
  它的用途是离线评估「本轮抽出的线索值不值得记」。
- 非法取值**回退到 `off`**（安全侧）。

## 记什么、不记什么

可持久化的键**只有 6 个**（`app/agent/preferences.py::TRACKED_KEYS`）：

| 键 | 类别 | 值域 | 推断禁区 |
|---|---|---|---|
| `usage` | usage | `engine._USAGE_HINTS` 的值（通勤 / 家庭 / 长途 / 商务） | 否 |
| `body_type` | constraint | `app/common/enums.py::BODY_TYPES` | **是** |
| `energy_preference` | constraint | `ENERGY_TYPES` + `new_energy` / `fuel` | **是** |
| `budget` | constraint | `{min, max}`（数值，min ≤ max） | **是** |
| `passengers` | constraint | 整数 1–20 | 否 |
| `avoid` | exclusion | 能源类型 ∪ 车身类型 ∪ `new_energy` / `fuel` | 否 |

**推断禁区**的判据只有一条：这些字段是**硬约束**，会直接下推 SQL
（`body_type IN (...)`、`energy_type IN (...)`、价格区间）。模型猜错一个车身类型，
整个候选集就空了，而用户从没要求过——这与本仓早已拒绝的「把『我最看重安全』
映射到某个维度」是同一类错误：**用行为冒充理解**。

### 刻意不记的

| 不记什么 | 为什么 | 何时做 |
|---|---|---|
| **权重**（`profile.weights`） | `merge_profile` 的权重是**累加**语义（`profile.weights[dim] += w`）。跨会话恢复会与本轮的累加叠加成「一句话叠三轮」。「恢复的是绝对值还是增量」必须先定义 | Phase 2 |
| **模型推断的偏好** | 见上。LLM 抽取（L1 `soft_prefs`）的产物**不进**记忆 | 永不（除非先证明推断可信） |
| **品牌约束**（`brand_ids` / `brand_exclude_ids`） | 值是品牌 id，需要另一套词表与校验（品牌表是动态的）。Phase 1 先把「排除项」这件事在车身/能源维度上做通 | Phase 2 |
| **行为类记忆**（`source_type=behavioral`） | 还没有定义「什么样的行为算偏好」 | Phase 3 |
| **显式取消语义**（「预算不限」） | 正则解析不出「取消」，旧预算会留在记忆里 | Phase 2（当前只能逐条 `DELETE`） |

## 时序：回填发生在哪一步

```
extract_hints(message)          ← 确定性正则，抽自用户原话
  ↓
restore_into_profile(记忆)       ← 旧记忆**先**回填（且只填空缺）
  ↓
merge_profile(profile, hints)   ← 本轮线索**后**合并（因此本轮压过记忆）
  ↓
persist_hints(记忆)             ← 把「记忆 + 本轮」的有效值写回
```

三条理由，各对应一个具体故障：

1. **回填在合并之前**：否则记忆会盖住本轮——本轮说「SUV 也行」，
   记忆里还挂着「不要 SUV」，用户的正面诉求被旧记忆压掉。
2. **回填只填空缺，不覆盖会话已有的值**：会话内的画像是**比记忆新**的事实。
   无条件覆盖会让「今天说 30 万」在下一轮被「记忆里的 20 万」悄悄改回去。
3. **写入的是「有效值」而不是「本轮线索」**：有效值 = 记忆 ⊕ 本轮（走
   `merge_profile` 算出来），所以「上个月说过的不要 X」不会被「这轮说的不要 Y」
   冲掉——排除项取并集。

## 矛盾消解：同一个值不能既被要求又被排除

`body_type IN ('suv') AND body_type NOT IN ('suv')` = **候选集恒空**，
而用户只看到一句「没有合适的车」。

消解规则（**只有一处实现**：`preferences.py::_resolve_conflicts`）：

| 层 | 含义 | 优先级 |
|---|---|---|
| 1 | 本轮用户明说的 | 最高 |
| 2 | 本会话已经说出口的 | 次之 |
| 3 | 记忆里存的 | 最低 |

- 同一个值出现在多层时，**靠前（更新）的层说了算**；
- 同一层内既要求又排除 → 以**排除**为准（宁可少推一台车，好过推一台用户明确排斥的车）。

上表三层是**回填**路径（`restore_into_profile`）。**写入**路径只有两层——本轮 vs
「记忆 ⊕ 本轮」：它从**空画像**起算（`effective_tracked`），会话里此前累积的值
不参与写入，写进去的始终是「记忆 + 本轮」算出的有效值。

> 此前这条规则在调用点上有**两遍**（一遍按「本轮的排除」过滤、一遍按「最终排除集」
> 过滤），两遍规则不同、互相掩盖——变异测试因此漏掉了两处。规则散落 = 规则漂移。

## 覆盖语义：一个键只留一行

同一 `(identity, category, key)` 写新值即覆盖旧值，**行 id 稳定**
（用户「删掉这条记忆」删的是同一条）。唯一约束
`(identity_id, category, key, value)` 因此不挡「同键不同值」——
覆盖由写入逻辑保证。

记忆是「**当前状态**」，不是「流水账」：用户改了主意，旧值直接被新值取代。

## 身份与凭据

- 匿名身份走 **`Authorization: Bearer <token>`**（与本仓既有登录态同一套凭据机制，
  不引入第二套，也免掉 Cookie 的 CSRF 面）。
- **库里只存 token 的哈希**（sha256），明文只在新建时给一次——之后无法再取回
  （明文可重置，泄漏的哈希不可反推）。
- 带着有效 token 再调 `POST /agent/identity` 会返回**同一个身份**，
  不凭空多一个（否则客户端重装页面就让旧记忆失联了）。
- 两张表（`anonymous_identities` / `preference_memories`）由迁移 `d3e8f1a4b7c2` 建立。
  生产走 `alembic upgrade head`，本地开发走 `create_all`——两条建表路径只要有一处
  不一致，缺陷就**只在生产暴露**。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/agent/identity` | 领取匿名身份，返回 `token`（只在新建时非空） |
| `GET` | `/agent/preferences` | 列出该身份已记住的偏好（未带身份返回**空列表**，不是 401） |
| `DELETE` | `/agent/preferences/{memory_id}` | 删掉一条记忆（只删自己的；别人的返回 404） |

未携带身份或身份无效时，记忆相关接口一律**返回空结果而不是 401**——
前端可以无条件调用，不必先领身份才能问车。

## A/B 实测证据

### 变异测试：15 个变异点，15 个全被抓住

「测试全绿」不等于「测试有效」。逐个把实现改坏，确认对应用例会变红
（脚本：`.tools/mutate_prefs.py`，本地专属、不入库）：

| 变异点 | 抓住它的用例 |
|---|---|
| off 模式仍然回填 | `test_off_mode_does_not_restore` |
| 回填覆盖会话已有的预算 | `test_restore_does_not_override_the_session_value` |
| 回填不认本轮的正面诉求 | `test_restore_drops_exclusion_when_this_turn_asks_for_it` |
| 写入不认本轮的正面诉求 | `test_positive_turn_clears_the_older_exclusion` |
| 推断禁区失效 | `test_inferred_cannot_write_hard_constraints` |
| 封闭词表失效（值域不查） | `test_value_outside_the_vocabulary_is_rejected` |
| **记忆不提交**（写库等于没写） | `test_second_session_does_not_ask_the_budget_again` |
| 权重被当成可持久化维度 | `test_persist_does_not_persist_weights` |
| token 明文入库 | `test_identity_stores_only_the_token_hash` |
| 删记忆不校验归属 | `test_delete_preference_is_scoped_to_the_identity` |
| off/shadow 也写库 | `test_off_mode_writes_nothing` |
| 未确认的记忆也写库 | `test_unconfirmed_memory_is_not_written` |
| 有效值消失时不删旧记忆 | `test_negative_turn_clears_the_older_requirement` |
| 回填不填空缺 | `test_restore_does_not_override_the_session_body_type` |
| off 模式仍然查身份 | `test_gate_off_does_not_even_resolve_the_identity` |

> **变异脚本自己也会空转**：`-k` 选择串写错（用例改名了）时，pytest 退出码是
> 5（没收集到任何用例）而不是 1——脚本若只看「非零即算抓住」，就会把
> 「根本没跑到用例」记成一次成功捕获。首轮就有 1 个这样的假阳性
> （`test_weights_are_not_persisted` 与真实用例名 `test_persist_does_not_persist_weights`
> 对不上）。现在脚本把退出码 5 判为**判定无效**并跳过，且跑完自检
> 「被改动的文件必须逐字节还原」——变异脚本被打断时会把文件留在变异态，
> 后面的人会对着一个「被改坏但看起来正常」的代码跑测试（实测踩过）。

其中「记忆不提交」这一条来自一个**真实的坑**：`app/common/database.py::get_session`
**不提交**，请求结束即回滚。只 flush 不 commit 等于没写。端到端用例用
「回滚一次，记忆还在吗」来验证它真的落了盘。

### 端到端判据

真后端 + 真结构断言：**第一段对话说了预算，第二段对话不再问预算**。

```
第一段（带身份）：「预算12万」
第二段（带身份，同一 token，新会话）：「我想买台车」
  → clarification.missing 不含 budget
```

对照组两条：不带身份 → 仍然问 budget；闸门关着 → 仍然问 budget。
（证明「不再问」不是因为「问了就不问」，而是因为记忆真的带过去了。）

## 已知问题（不属本分支，另立）

**会话内的「既要求又排除」是本仓既有缺陷**：`merge_profile` 从不清除旧的正面诉求，
所以同一段会话里先说「要 SUV」再说「不要 SUV」，画像会同时留着
`body_type=['suv']` 与 `avoid=['suv']` → 候选集恒空。

本分支**不顺手修**它：修了会让「同一个对话在有/无身份时行为不同」。
记忆侧已经做到「不引入新的矛盾」，会话侧的老账另算。

## 相关分支

| 分支 | 关系 |
|---|---|
| 「docs/adr-and-glossary」 | 提供 ADR-0001（本分支实现的口径来源） |
| 「refactor/knowledge-consolidation」 | 把 `merge_profile` 搬到 `app.agent.profile`；本分支的 `preferences.py` 取 `app.agent.engine` 的同名 re-export，**两个布局都指向同一实现** |
| 「feat/multiturn-eval」 | 多轮评测框架；Phase 2 的记忆效果评测挂在它上面 |
