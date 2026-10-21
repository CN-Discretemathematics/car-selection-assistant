# 生产实况核查复盘（2026-10-21）

> 起因：用户问「已全部 merge 到 branch 上，生产库 IP 已加白名单」——
> 于是去核对生产实况，结果挖出**三件没人知道的事**，其中两件让一大块功能**在产品上不可达**。
>
> 三个分支对应三处修法：
> [fix/migration-boolean-default-postgres](https://github.com/CN-Discretemathematics/car-selection-assistant/compare/main...fix/migration-boolean-default-postgres)、
> [fix/deploy-script-stale-sha](https://github.com/CN-Discretemathematics/car-selection-assistant/compare/main...fix/deploy-script-stale-sha)、
> `feat/frontend-identity-wiring`（本分支）。

## 一句话

**「部署成功」不等于「功能在跑」。** 今天把生产从 13 天前追回来之后，
发现这一轮工作里有**一整块功能从未在产品上可达**——而且**线上没有任何报错**。

## 时间线

| 时刻 | 事件 |
| --- | --- |
| 2026-10-08 05:03 | 最后一次成功部署（SHA `621833da`） |
| 10-09 ~ 10-21 每天 05:00 | 部署脚本记「已是最新（621833da）」——**但 main 早已前进几十笔** |
| 2026-10-21 14:49 | 手工部署 `f1ac188` ⇒ **容器起不来**（迁移在 PostgreSQL 上报错）⇒ 脚本自动回滚，站点全程 200 |
| 15:16 | 修好迁移并应用 ⇒ 重新部署 ⇒ **上线成功**（站内外 `commit_sha` 一致） |
| 15:25 | 线上部署脚本热修（仓库 + 线上双改），服务器上跑 7 场景测试 7/7 过 |
| 16:02 | 人工确认：生产聊天面板**能正常打开**（浏览器自动化点不开是工具问题，不是站点问题） |

## 发现一：自动部署**静默**跳过了 13 天

**现象**：13 天里，站点一直跑着 10-08 的版本，而 `main` 上已经有 durable memory、
拒绝反馈、账户合并、多轮评测——**全部没上线**。

**根因**（不是脚本逻辑写错）：脚本把**一次未认证的 API 读**当成了唯一事实源：

```bash
remote_sha=$(curl -fsSL -m 30 ".../commits/main" | sed -n 's/.*"sha": *"\(...\)".*/\1/p')
[ "$remote_sha" = "$current_sha" ] && log "已是最新（${remote_sha}），无需部署"
```

那次读到的是**陈旧响应**（疑似中间链路的 HTTP 缓存）⇒ 「远端 == 已部署」在陈旧读上**恒为真**
⇒ 每天安静地跳过。**日志里没有任何异常**——它打印的是一句像模像样的「已是最新」。

**修法**（`deploy/carsel-deploy.sh`，三重防护，各自对应一个独立失效模式）：

| 层 | 机制 | 挡住什么 |
| --- | --- | --- |
| ① | API 读带 **cache-buster**（URL 每次唯一）+ `Cache-Control: no-cache` | HTTP 缓存返回陈旧响应（**治本**） |
| ② | **两个独立端点**交叉核对（`/commits/{branch}` 与 `/branches/{branch}`） | 某一个端点被缓存 |
| ③ | **源码包提交时间** vs API 提交时间（codeload 包的 mtime 就是它那个提交的提交时间），容差 6h | 两者都陈旧时至少有一方露馅 |

外加**停滞告警**（状态文件超过 5 天没更新却读到「远端 == 已部署」⇒ 不走快路径，改用源码包核对）
与**不猜**原则（任一处对不上 ⇒ 报错退出 1，不部署、不谎报「已是最新」）。

**A/B 证据**（[部署脚本的 7 场景测试](../deploy/tests/verify_deploy_stale_sha.sh)，桩 curl/docker/rsync）：

- 喂**旧脚本**：事故场景打印「已是最新」并 exit 0，**还会把陈旧代码真的构建部署**；
- 喂**新脚本**：7/7 拦住。同一套测试在**生产服务器**（真 flock、真 tar）上也是 7/7。

## 发现二：一条只在 PostgreSQL 上会炸的迁移

**现象**：第一次部署 `f1ac188` 时容器起不来：

```
sqlalchemy.exc.ProgrammingError: column "confirmed" is of type boolean
  but default expression is of type integer
```

**根因**：迁移 `d3e8f1a4b7c2` 里 `confirmed BOOLEAN DEFAULT 0`。
**SQLite 接受** `DEFAULT 0`（它不区分 0 与 false），**PostgreSQL 不接受**。
而本机测试库与 **CI 的测试库都是 SQLite**（CI 里**没有 PostgreSQL 服务**）
⇒ 这条 DDL **从来没在生产数据库上跑过**。

`docs/2026-10-20-durable-preference-memory.md` 早就写过这句话：

> 生产走 `alembic upgrade head`，本地开发走 `create_all`——两条建表路径只要有一处不一致，
> 缺陷就**只在生产暴露**。

这次就是那句话应验。

**修法**：`server_default=sa.text("false")` + **一条能抓住这类问题的门禁**
（[迁移的 PostgreSQL 方言门禁](../backend/tests/test_migration_postgres_compat.py)）：

1. 把**整条迁移链**在 **PostgreSQL 方言**下离线渲染成 DDL（不连库）；
2. 把**模型元数据**（`create_all` 路径）也在 PostgreSQL 方言下渲染；
3. 断言**布尔列的默认值必须是布尔字面量**（事故根因的直接判据）；
4. 断言「渲染确实覆盖了全部迁移」——否则第 3 条会**空转**。

**变异实测**：把 `false` 改回 `0` ⇒ 门禁立刻红并指到具体列
（「迁移路径: 列 confirmed 的默认值 '0' 不是布尔字面量」）。

## 发现三：前端**从未领身份** ⇒ 整套记忆能力在产品上不可达

**现象**：上线后生产库里 `anonymous_identities` / `preference_memories` /
`variant_feedbacks` **全是 0 行**。

**根因**（两层叠加）：

1. **接线缺失**：前端 `web/lib/agent.ts` 发消息时**只带 `Content-Type`**，
   全 `web/` 目录搜 `identity` **零命中** ⇒ 后端 `identity=None`
   ⇒ 记忆**不写不读**、拒绝**不记录**、合并**无从谈起**。
   而后端早就备好了（`Authorization: Bearer <identity token>` → `_resolve_identity`），
   docstring 还写着「前端没领身份也能正常问车，只是没有跨会话记忆」——
   **缺身份是合法请求 ⇒ 线上没有任何报错**。
2. **闸门关着**：生产 `.env` 里**没有** `DURABLE_PREFS_MODE` ⇒ 默认 `off`
   ⇒ 即使接线了，记忆也不写不读。

**修法**（本分支 `feat/frontend-identity-wiring`）：

- 前端：打开聊天时**领一次身份**（token 存 localStorage），
  所有 agent 请求带 `Authorization: Bearer <token>`；
  领不到就按「无身份」发消息——**身份是增强项，不能让它把问车带挂**；
- 生产闸门：按拍板切到 **shadow**（只落日志、不写库、不改变推荐），观察后再切 `on`；
- 隐私页：补上「匿名标识 + 偏好记忆」的说明（不能一边开始记偏好、
  一边写着「不与你个人身份关联」）。

**验证**：`web/lib/agent.test.mts` 8 条用例（复用已有 token / 并发去重 / 失败降级 /
带头与不带头 / 清除本地身份）；生产实测
`POST /agent/identity` → 201 → 带 `Authorization` 发消息 → 200 返回真实推荐。

## 发现四：`shadow` 档是**空档**——它什么都不观察

**现象**：把生产闸门切到 `shadow` 之后，日志里**一条影子记录都没有**，表里也没有记忆。

**根因**：路由层用 `is_enabled()`（**只在 `on` 为真**）决定「是否解析身份」，
而两条影子日志——「偏好记忆（shadow，未写库）」与「拒绝反馈（shadow，未写库）」——
**都以「有身份」为前提**（开头都是 `if identity is None: return`）
⇒ `shadow` 档的身份恒为 None ⇒ 影子日志**永远不会触发**
⇒ 这一档**不写库（对）也不观察（错）**。

⇒ 「先观察一两天再切 on」那道观察期于是是**假的**：切 on 之前根本看不到任何东西。

**修法**（分支 `fix/shadow-mode-actually-observes`）：

- 新增 `is_observed()`（`shadow` 与 `on` 都为真，`off` 为假）；
- 路由改用它解析身份。**只有这一处变了**——`restore_into_profile` 与
  `effective_brand_exclusions` 仍用 `is_enabled()` ⇒ `shadow` 档**照旧不回填、不改变推荐**
  （观察 ≠ 影响 ✓）。

**A/B**：把路由改回 `is_enabled()`（旧代码）⇒ 新增的 3 条用例**全红**；改回来 ⇒ 全绿。

⚠️ **注意**：这条修复合入并部署**之前**，生产上的 shadow 观察期**仍然是假的**。

## 发现五：歧义品牌的**排除项**静默失效（「不要大众」不算数）

**现象**：给题库补上「**排除项**跨会话」这一类场景后，on 档有 **3/23 条红**，
而失败的**全是「大众」**这一个品牌。

**根因**（两处，同一个知识切成了两半）：

1. `catalog/brands.py::resolve_brand_mentions` 里，「大众」属于 `AMBIGUOUS_BRANDS`
   （日常词歧义品牌：理想/长安/大众/未来/启辰/东风）⇒ 硬约束用途要求**带强意图词**
   （"我想买大众"算，"大众化"不算）。可这个守卫排在**否定判定之前**
   ⇒「不要大众」没有第二种解释（用户明说不要某品牌），却被当日常词丢掉
   ⇒ **品牌排除项根本没进画像**。
2. 回填路径 `agent/preferences.py::resolve_brand_names` 按**正向约束**解析记忆里存的
   品牌名 ⇒ 同样撞上歧义守卫 ⇒ `resolve_brand_names(['大众'])` 返回 `([], [])`
   ⇒ 即使记忆写进去了，**回填时也解析不出来** ⇒ 排除项静默失效。

⇒ 全程**没有任何报错**：用户说「不要大众」，系统照常推荐大众的车。
⇒ 而这正是计划 §七那条判断（「**硬约束排除项**是记忆的第一价值」）**最常落地的场景**。

**修法**：

1. 歧义守卫**在否定语境下不适用**——否定判定提到守卫之前（否定语境没有歧义）；
   非否定语境的既有保护**逐字不变**（"大众的预算 20 万" 仍不被当作品牌）。
2. `resolve_brand_names` 统一按**排除语境**解析（`不要 {name}`）——
   记忆里存的就是「用户明说不要的品牌」，那个语境没有歧义。

**验证**：`tests/test_brand_constraint.py` + `tests/test_preferences.py` 各一条回归用例
（后者带**前提断言**：没有记忆时「大众」确实会被推荐，否则测的是空气）。

## 连带结论

1. **「部署成功」≠「功能在跑」**：今天之前，
   durable memory / 拒绝反馈 / 账户合并 / 隐私导出这**一整轮工作**（4 个分支、几十笔提交）
   在产品上是**暗的**——后端全做完了，前端一行没接。
   ⇒ 上线检查清单要加一条：**「这个功能的前端入口在哪？它会被谁触发？」**
2. **「接口有返回」≠「数据会落」**：缺身份是合法请求，闸门默认 off，
   两者都不会报错。要验证一个功能**真的在跑**，得看**表里的行数**，
   而不是看接口 200。
3. **合成数据与真实数据的差别会体现在「零流量」上**：今天差点误判成
   「用户不聊天」，真相是「**产品上没有能记住东西的入口**」。
4. **「中间档」也可能是空的**：三模式闸门（off / shadow / on）看着周全，
   中间那档却因为「身份不被解析」而**一条日志都不落**。
   ⇒ 加档位时必须问一句：**这一档具体多做了什么？**（写代码时也要问：谁在这一档被解析、被记录？）
5. **「防护规则」可能把正确用法一起挡掉**：歧义守卫（"大众"既是品牌也是日常词）
   本来是防误判，却把**否定语境**也挡了 ⇒ 用户明说的排除项静默失效。
   ⇒ 加任何「保守拒绝」规则时，必须问一句：**它在用户明确表达的场景下会不会也拒绝？**
   （这类缺陷的特征是：没有任何报错，只是「没生效」。）

## 还欠着的

| 项 | 状态 |
| --- | --- |
| `shadow` 空档修复上线 | **已修未上线**（分支 `fix/shadow-mode-actually-observes`）——合入并部署前，生产的观察期**仍然是假的** |
| 生产闸门切 `on` | 等 shadow 空档修复合入 + 影子日志攒够再决定 |
| CI 加 **PostgreSQL 服务**并真跑 `alembic upgrade head` | **未做**（这才是发现二的终局防线） |
| 「排除项跨会话」评测场景（品牌/车系排除现在**没有**评测级覆盖） | **未做** |
| 「硬约束排除项是记忆的第一价值」的**真实用户验证** | **未做**（生产至今零真实对话流量） |
| 「我记住的偏好」列表与撤回入口（后端 `GET/DELETE /agent/preferences` 已就绪） | **未做**（本轮只做最小接线） |
| 首轮回答 11~16 秒（embedding→检索→rerank→LLM 一条链） | 未排期 |
| 生产库口令进了会话输出，建议轮换 | 待办 |

## 参考

- [待立项项落地方案](2026-10-21-deferred-items-execution-plan.md)（口径已按本次复盘修正）
- [记忆的边际价值](2026-10-21-memory-value-offline-replay.md)（第 4 项的机制层证据）
- [PostgreSQL 布尔默认值事故](2026-10-21-postgres-boolean-default.md)（发现二的细节）
- [部署与自动更新手册](deployment.md) §2.1（三重防护的说明）
