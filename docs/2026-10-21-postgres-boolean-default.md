# PostgreSQL 布尔默认值把生产部署挡下来了（2026-10-21）

> 分支：`fix/migration-boolean-default-postgres`｜事故发生：2026-10-21 14:49（部署时）
> 根因一句话：**`BOOLEAN DEFAULT 0` 在 PostgreSQL 上直接报错，而 SQLite 接受**——
> 于是本机与 CI 一路绿灯，这个迁移**从来没在生产数据库上跑过一次**。

## 一、事故经过

2026-10-21 上午查明「生产停在 13 天前」后，决定把 `main` 部署到生产
（服务器 `121.41.4.12`，SHA `621833da` → `f1ac188`）。

部署脚本的流程是：同步源码 → 重建镜像 → `docker compose up -d` → 健康检查 → 失败自动回滚。
其中 `deploy/backend.Dockerfile` 的 CMD 是：

```dockerfile
CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn app.main:app ..."]
```

⇒ **迁移在容器启动时自动执行**，迁移失败 ⇒ 容器根本起不来 ⇒ 健康检查失败 ⇒ 自动回滚镜像。

实测（镜像 `60dc1d86`，生产库 `car`）：

```
2026-10-21 14:59:01 镜像构建完成（commit=f1ac18839a140b45b03f385999e5ac9ee5678df0）
2026-10-21 14:59:20 ERROR 容器启动失败，尝试回滚
```

把同一个镜像单独跑一次迁移，真实报错是：

```
sqlalchemy.exc.ProgrammingError: (psycopg.errors.DatatypeMismatch)
  column "confirmed" is of type boolean but default expression is of type integer
HINT: You will need to rewrite or cast the expression.
[SQL: CREATE TABLE preference_memories ( ... confirmed BOOLEAN DEFAULT 0 NOT NULL, ... )]
```

⇒ 迁移事务整体回滚 ⇒ 生产库仍停在 `f1a2b3c4d5e6`，
`preference_memories` / `variant_feedbacks` / `anonymous_identities` 三张表**一张都没建上**。

**回滚机制是有效的**：站点全程可用（`ready=200 site=200`），只是跑的还是 13 天前那一版。

## 二、根因：两条建表路径不一致

`docs/2026-10-20-durable-preference-memory.md` 早就写过这句话：

> 生产走 `alembic upgrade head`，本地开发走 `create_all`——两条建表路径只要有一处不一致，
> 缺陷就**只在生产暴露**。

这次就是那句话应验，具体差在哪：

| 路径 | `confirmed` 的默认值 | PostgreSQL 接受吗 |
| --- | --- | --- |
| `create_all`（模型 `app/common/models.py`） | **没有 server_default**（只在 Python 侧 `default=False`） | ✅ 接受 |
| `alembic upgrade head`（迁移 `d3e8f1a4b7c2`） | `server_default=sa.text("0")` | ❌ 拒绝 |

**为什么本机与 CI 测不出来**：本机测试库与 CI 的测试库都是 **SQLite**，
而 SQLite 接受 `BOOLEAN DEFAULT 0`（它不区分 0 与 false）。
CI 里也**没有 PostgreSQL 服务**（`.github/workflows/ci.yml` 的 backend job 只装 requirements 跑 pytest）。

⇒ 一条**只在生产数据库上才会炸**的 DDL，躲过了全部 1584 个用例。

## 三、修法

### 1. 迁移改用布尔字面量

```python
sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false"))
```

`false` 是 PostgreSQL 的布尔字面量，SQLite 也接受 ⇒ 两条路径都通。

### 2. 加一条**能抓住这类问题**的门禁

`backend/tests/test_migration_postgres_compat.py`（4 条用例）：

1. 把**整条迁移链**在 **PostgreSQL 方言**下离线渲染成 DDL
   （`alembic upgrade head --sql`，不连库）；
2. 把**模型元数据**（`create_all` 那条路径）也在 PostgreSQL 方言下渲染；
3. 断言**布尔列的默认值必须是布尔字面量**（`true`/`false`）——这是事故根因的直接判据；
4. 断言「渲染确实覆盖了全部迁移」（渲染出的 `CREATE TABLE` 不少于源码里 `op.create_table` 的次数）
   ——**否则第 3 条会空转**（本仓硬规矩：空转断言必须被变异测试抓出来）。

**变异实测**（`.tools/mutate_pg_bool_default.py`）：把 `false` 改回 `0`，
门禁立刻红并指到具体列：

```
E  迁移路径: 列 confirmed 的默认值 '0' 不是布尔字面量
```

### 3. 两条路径都要过

门禁同时渲染**迁移路径**与 **`create_all` 路径**——
这正是「两条建表路径只要有一处不一致，缺陷就只在生产暴露」那句话的机械兑现。

## 四、两个踩坑（都写进测试注释里）

1. **alembic 离线模式的 SQL 走 `config.output_buffer`，不是 `config.stdout`**
   （alembic 库内的 `environment.py`：`elif self.config.output_buffer is not None: opts["output_buffer"] = ...`）。
   只设 stdout ⇒ 渲染结果整段丢失 ⇒ 「CREATE TABLE 不在结果里」会变成一条**恒真的空转断言**。
2. **alembic 的 `env.py` 调 `fileConfig(...)`，它默认 `disable_existing_loggers=True`**，
   会掐掉 pytest 的 caplog ⇒ 后续 20 个用例因收不到日志而失败。
   测试里用 `MonkeyPatch` 把 `logging.config.fileConfig` 换成空函数，并在结束时 `undo()`。

## 五、还欠着的（不在本笔）

- **CI 里加一个 PostgreSQL 服务并真跑 `alembic upgrade head`**：
  本笔的门禁抓的是「布尔默认值」这一类；它抓不到的还有索引名冲突、外键指向不存在的表等。
  真正的终局是让 CI 有一条**连真 PostgreSQL 跑迁移**的 job。
- **生产库当时还缺 3 张表**（本笔修好后由部署脚本的容器启动流程自动建）。
