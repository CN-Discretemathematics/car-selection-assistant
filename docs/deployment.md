# 部署与自动更新手册

本文记录生产服务器的**实际**形态与部署方式，以及「只部署 main」的自动更新机制。

## 1. 生产服务器实际形态

| 项 | 现状 |
|---|---|
| 主机 | 阿里云轻量应用服务器（2C2G，Alinux 3），公网 `121.41.4.12` |
| 目录 | `/srv/carsel`（**文件同步式**，服务器上**没有 git**） |
| 运行 | Docker Compose 三服务：`deploy-api` / `deploy-web` / `deploy-redis`，端口只绑 `127.0.0.1` |
| 入口 | 宿主 nginx（`:80`）→ `/` 与 `/api` 反代到容器；备案通过后再上 443/HSTS |
| 配置 | `/srv/carsel/backend/.env`（600，**不入库、不被部署覆盖**） |

> README 早期写的 `git clone <repo> /srv/carsel` 与实际不符：服务器上不装 git，
> GitHub 也只有 `api.github.com` / `codeload.github.com` 可达（`github.com` 会超时）。
> 源码同步一律走 GitHub 的源码包（codeload tarball），不依赖 git。

## 2. 自动更新：只部署 main

脚本：`deploy/carsel-deploy.sh`（服务器上安装为 `/usr/local/bin/carsel-deploy.sh`）。
定时：`0 5 * * *`（每天早上 5 点扫一次 `main`）。

```bash
carsel-deploy.sh             # 对比 main 最新提交，有更新则同步源码 → 重建镜像 → 切流 → 健康检查
carsel-deploy.sh --check     # 只打印「远端 main SHA / 已部署 SHA / 是否有更新」
carsel-deploy.sh --dry-run   # 走完下载、解包与 rsync 预览，不构建、不重启（核对删除清单用）
carsel-deploy.sh --force     # 忽略 SHA 相同，强制重部署（手工回滚/重发布用）
```

流程与保障：

1. **只认 main**：通过 `api.github.com` 取 `main` 最新 SHA，与 `/var/lib/carsel/deployed-main.sha`
   比对；相同则直接退出（幂等，不产生任何变更）。
2. **源码包**：`codeload.github.com/.../tar.gz/refs/heads/main`（公开仓库，无需凭据；
   仓库内不含 `.env`）。
3. **同步**：`rsync -a --delete` 镜像到 `/srv/carsel`，让「仓库里删掉的文件」也同步删除。
4. **构建失败不影响线上**：`docker compose build` 失败时**不重启容器**，旧版本继续服务。
5. **健康检查**：切流后轮询 `/api/v1/ready`（期望 200）与站点首页（期望 200），最多 2 分钟。
6. **自动回滚**：切流前把当前镜像打上 `deploy-api:rollback` / `deploy-web:rollback`；
   健康检查失败即回滚这两个 tag 并 `up -d`，同时日志记录。
7. **单实例**：`flock` 防重入，不会与手工部署或上一次未完成的部署叠加。
8. **留痕**：`/var/lib/carsel/last-deploy.json`（sha / previous / 时间 / 状态）、
   `/var/log/carsel-deploy.log`（自动截断保留最近 2000 行）。

### 服务器本地数据绝不被覆盖或删除（rsync 排除清单）

| 排除项 | 原因 |
|---|---|
| `backend/.env`、`backend/.env.*` | 生产凭据与历史备份 |
| `backend/.tmp/` | **compose 挂载卷**：embedding 向量缓存 / dense 水印，删掉会触发昂贵重建 |
| `backend/eval/` | 内部笔记（有意不入库） |
| `backend/snapshots/`、`backend/vendor/`、`backend/.venv/`、`backend/.embed_cache.json` | 运行期/本地依赖产物 |
| `logs/`、`*.log`、`*.tar.gz` | 日志与备份包 |
| `reviewer/REVIEWER_AGENT.md` | 有意 gitignore 的内部评审规范 |
| `node_modules/`、`.next/` | 本地构建产物 |

改动排除清单后，务必先 `--dry-run` 并核对「将被删除的项」列表。

## 3. 手工部署与回滚

```bash
# 手工部署（例如刚合并、不想等到 5 点）
ssh root@121.41.4.12 '/usr/local/bin/carsel-deploy.sh --force'

# 只看状态
ssh root@121.41.4.12 '/usr/local/bin/carsel-deploy.sh --check'

# 回滚到上一版镜像（脚本自动回滚用同一机制）
ssh root@121.41.4.12 'cd /srv/carsel/deploy && \
  docker tag deploy-api:rollback deploy-api:latest && \
  docker tag deploy-web:rollback deploy-web:latest && docker compose up -d'

# 从本机手工推源码（自动化不可用时的兜底；服务器无 git，用仓库源码包）
git archive --format=tar.gz -o /tmp/src.tar.gz <commit>
scp -i .deploy/ecs_key /tmp/src.tar.gz root@121.41.4.12:/tmp/
ssh -i .deploy/ecs_key root@121.41.4.12 'cd /srv/carsel && tar -xzf /tmp/src.tar.gz && \
  rm -f /tmp/src.tar.gz && cd deploy && docker compose build api web && docker compose up -d'
```

## 4. 新服务器接入这套机制

```bash
# 1) 目录与配置（.env 手工放置，600）
mkdir -p /srv/carsel /var/lib/carsel
# 2) 首次同步源码（可从本机 git archive 推，或直接跑一次部署脚本）
# 3) 安装部署脚本与定时任务
install -m 755 /srv/carsel/deploy/carsel-deploy.sh /usr/local/bin/carsel-deploy.sh
echo '0 5 * * * /usr/local/bin/carsel-deploy.sh >> /var/log/carsel-deploy.log 2>&1' >> /var/cache/cron.tmp
crontab /var/cache/cron.tmp && rm -f /var/cache/cron.tmp
# 4) 验证
/usr/local/bin/carsel-deploy.sh --check
```

## 5. 夜间任务与向量索引重建

脚本：`deploy/carsel-nightly.sh`（服务器上安装为 `/usr/local/bin/carsel-nightly.sh`）。
定时：`30 2 * * *`（cron 另有 `*/5` 看门狗与 `0 5` 自动部署）。

```bash
carsel-nightly.sh                 # 正常：销量导入（幂等）→ 仅当有新月份才重建索引
carsel-nightly.sh --force         # 数据回补/手工修数后强制重建一次
carsel-nightly.sh --no-rebuild    # 只导入不重建（排障用）
```

流程与要点：

1. **销量导入**：`docker exec deploy-api-1 python tools/fetch_sales_scheduled.py`（幂等，
   目标月已就绪时零操作；未发布则次日重试）。
2. **重建触发**：只有导入成功写入 `.tmp/sales-changed.flag`（新月份到位）才重建；
   `--force` 忽略标记。
3. **稠密（Zilliz）**：`tools/build_retrieval_index.py --target dense` 全量重灌，并写水位标记
   `.tmp/dense-build-meta.json`（含构建时的**销量月份 + 库内规模**）。
4. **稀疏（BM25）**：**必须走管理接口** `POST /admin/rag/reindex {"target":"sparse"}`——
   在运行中的 api 进程内重建。另起进程跑 `--target sparse` 建的索引会随子进程退出而丢弃
   （生产 `RETRIEVAL_BACKEND=milvus` 时，按数据量自动重建是关闭的）。
5. **重建后核对**：脚本自动打印 `/admin/rag/status` 的稠密/稀疏切片数与
   `stale/stale_reason`（月份 + 规模双比对，见 §7）。
6. **自更新**：仓库里的脚本变化后，下次运行自动安装到 `/usr/local/bin`。
7. 日志：`logs/sales-cron.log`（任务级）与 `logs/rebuild-cron.log`（重建明细），自动截断保留 3000 行。

**为什么销量变化必须重建**：车系摘要切片文本含「YYYY-MM 月销量 N 辆」一句话
（`app/rag/ingest.py`），不重建则 RAG 回答里的销量是旧的（2026-09 实况：数据补齐后
集合仍停留在旧切片集，而水位只比月份故误报「新鲜」）。

## 6. 管理页面入口

- **RAG 流程管理**：`http://<服务器>/ops/rag`（备案前用 `http://121.41.4.12/ops/rag`）——
  流程图 / 运行状态（含水位置信）/ 试运行 / 运行轨迹 / 评测报告，可在页面上触发索引重建。
- **凭据**：页面右上角填 **Bearer token**，值为服务器 `/root/carsel-admin-token.txt`
  （600 权限）；填写后保存在浏览器 localStorage，页面内所有管理请求自动带上。
  查看命令：`ssh root@<服务器> 'cat /root/carsel-admin-token.txt'`。
- **明文传输提醒**：备案前站点是 HTTP，token 会明文过网。远程管理建议走 SSH 隧道：
  ```bash
  ssh -i .deploy/ecs_key -L 8080:127.0.0.1:3000 root@121.41.4.12   # 然后访问 http://127.0.0.1:8080/ops/rag
  ```
- 管理接口在无 token 时返回 401、未配置 token 时返回 503（可用 `curl -s -o /dev/null -w '%{http_code}'` 自查）。

## 7. 已知事项与后续改进

- **`web/public/` 曾被漏掉**：`deploy/frontend.Dockerfile` 会 `COPY .../web/public ./public`，
  但仓库此前未跟踪该目录——全新克隆构建必失败（服务器靠手工 `.gitkeep` 侥幸可用）。
  现已补 `web/public/.gitkeep` 入库；rsync 排除清单里的 `web/public/.gitkeep` 可一并移除。
- **索引水位必须同时看月份与规模**：`dense-build-meta.json` 记录构建时的 `db_counts`，
  `/admin/rag/status` 在规模漂移时给出「车系 908→1078」式原因。旧标记（无 `db_counts`）
  退回只比月份，不误报。
- **只部署 main 的代价**：未合并的改动不会上线（需要的验证放在 PR 阶段完成）。
- **可选的 CI 门禁**：目前 PR 阶段没有自动跑测试（254 用例只在本地/手工执行）。
  公开仓库可加 GitHub Actions 跑 `pytest` + `tsc`，让「自动部署 main」更有底气。
- **通知**：脚本只写日志与状态文件；如需微信/邮件通知，可在脚本末尾追加钩子。
