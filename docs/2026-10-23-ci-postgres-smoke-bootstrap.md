# CI 的「真 PostgreSQL 功能校验」从加入起就没绿过（2026-10-23）

> 事件：所有 PR 的 CI job「migrations + 功能校验（postgres）」全红。
> 分支：`fix/ci-postgres-smoke-bootstrap`

## 一句话

那个 job 跑 `python tools/smoke_durable_memory.py`，而脚本**从 `backend/tools/` 目录执行时
`sys.path[0]` 是那个目录、不是 `backend/`** ⇒ `import app.*` 必然失败
⇒ **job 自 2026-10-21（PR #85）加入起就没绿过** ⇒「真 PostgreSQL 上跑一遍功能校验」
那道防线**一直是空的**，而 CI 是红的。

## 为什么这事值得单独立档

一道**看起来在跑、实际从没跑过**的防线，比没有这道防线更坏：
它让人以为「真 PG 上的功能校验已经有人跑过了」——
而本地/CI 侧的实际状态是：真 PG 只在**本地手工**跑过（我这次在生产 RDS 的临时库上跑通了），
**CI 上一次都没跑过**。

## 根因（三个叠加）

1. **导入路径**：脚本漏了仓库早已存在的公共引导 `backend/tools/_bootstrap.py`
   （2026-10-02 抽取，29 个脚本在用；`from _bootstrap import ensure_backend_on_path`
   **导入即生效**）。
   ⇒ `sys.path[0]` 的问题对 `python tools/xxx.py` 这种入口**永远成立**，
   `working-directory: backend` 救不了它。
2. **闸门用 `setdefault`**：脚本用 `os.environ.setdefault("DURABLE_PREFS_MODE", "on")`
   ⇒ 环境里已有值就**沿用** ⇒ 在生产容器里（`shadow`）跑，记忆不写库 ⇒
   报出「记忆真的写进库 —— 写了 0 行」这类**误导性失败**（看起来像记忆功能坏了，
   实际只是闸门没开）。CI 里 env 是 `on`，所以这一条在 CI 上不显形。
3. **控制台编码**：打印「⇒」在 Windows GBK 控制台上 UnicodeEncodeError ⇒
   **本地跑必崩**（CI/服务器是 UTF-8 不受影响）。

## 改了什么

| # | 改动 | 位置 |
| --- | --- | --- |
| 1 | 加 `from _bootstrap import ensure_backend_on_path`（放在 `from app...` 之前） | `backend/tools/smoke_durable_memory.py` |
| 2 | **同一类缺陷**一并修（它连老的 `sys.path.insert` 都没有，直接跑同样报错） | `backend/tools/eval_memory_value.py` |
| 3 | 闸门改**强制赋值**（`os.environ["DURABLE_PREFS_MODE"] = "on"`）——本脚本的语义就是「校验记忆功能」，闸门必须是 on | `backend/tools/smoke_durable_memory.py` |
| 4 | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` 兜底 Windows 控制台 | 同上 |
| 5 | **AST 级契约测试**：凡模块级 import `app.*` / `tools.*` 的工具脚本，在那之前必须已经把 `backend/` 放进 `sys.path` | `backend/tests/test_tools_bootstrap.py`（新增 2 条） |

⇒ 第 5 条的意义：这类缺陷以后会**在本地被 pytest 抓到**，不必等它在 CI 上红。

## 验证（在**真 PostgreSQL** 上按 CI 的形态跑）

CI 的 job = checkout 完整仓库 → pip install → `cd backend` → `alembic upgrade head`
→ `python tools/smoke_durable_memory.py --seed-car`。

⇒ 在生产 RDS 上开**临时库** `smoke_ci_tmp`（生产库 `car` 完全不动，用完即删），
在**完整源码树**里跑同样两条命令：

| 步骤 | 结果 |
| --- | --- |
| `python -m alembic upgrade head` | **exit 0**（全部迁移在真 PG 上跑通） |
| `python tools/smoke_durable_memory.py --seed-car` | **12/12 通过，exit 0** |

⇒ **CI 那个 job 会绿。**

### 为什么必须用「完整源码树」

生产镜像的 Dockerfile 只 COPY 了 `backend/{alembic,alembic.ini,app,tools}`，
**没有 `backend/tests/`** ⇒ 脚本里的 `from tests.seed import ...`（种子数据）
在容器里必然失败 ⇒ **在生产容器里跑这个脚本不是 CI 的形态**（CI 是 checkout 完整仓库）。
⇒ 「CI 与生产不同构」这件事本身也值得记一笔：**能在容器里跑 ≠ CI 会跑**。

### 顺带记一笔：本地形态

SQLite 形态（`DATABASE_URL="sqlite:///.tmp/smoke.db" python tools/smoke_durable_memory.py --seed-car`）
同样真跑过：**12/12 通过，exit 0**。

## 边界

- 只改**导入路径**、环境变量处理与控制台编码，**不改脚本逻辑**、不改断言、不改 CI 配置；
- 另 4 个工具脚本（`eval_soft_prefs.py` / `eval_softpref_ab.py` /
  `eval_tradeoffs.py` / `replay_conversation.py`）用的是老写法
  `sys.path.insert(...)`（能跑，只是留了 E402 noqa）⇒ 契约测试放它们过；
  收拢到 `_bootstrap` 是**可选的清理**，不在本次范围内。
