# 阿里云运维手册（本项目）

面向本项目在阿里云上的实际资源，说明**哪些运维在控制台做、哪些必须 SSH**、操作顺序与已知坑。
配套文档：[deployment.md](deployment.md)（部署与自动更新、管理入口）、[credential-rotation.md](credential-rotation.md)（凭据轮换）。

---

## 0. 先看结论：控制台 vs SSH

| 运维动作 | 控制台可做 | 必须 SSH |
|---|---|---|
| 重启/关机实例、VNC 远程连接 | ✅ 轻量控制台 | — |
| 防火墙放行端口（80/443/22…） | ✅ 轻量控制台（**CLI 无权限**） | — |
| 实例快照（手动/自动策略）、监控看板 | ✅ 轻量控制台 | — |
| 实例续费、升降配 | ✅ 轻量控制台 | — |
| RDS 续费、改规格、删除保护 | ✅ RDS 控制台 | — |
| RDS 账号/口令、白名单、参数 | ✅ RDS 控制台 | 改完**必须 SSH** 同步 `.env` 并重建容器 |
| RDS 备份/恢复（含按时间点） | ✅ RDS 控制台 | 恢复后需 SSH 改 `.env` 验证 |
| OSS 生命周期、存储类型、监控 | ✅ OSS 控制台 | — |
| RAM 用户 / AccessKey / 授权 | ✅ RAM 控制台 | 换 OSS 凭据后需 SSH 同步 `.env` + 重建容器 |
| 域名解析、实名、**ICP 备案**、证书 | ✅ 域名/备案控制台 | 已完成（2026-09）：nginx `server_name` + 443 已上线 |
| 费用账单、预算告警、云监控告警 | ✅ 费用中心 / 云监控 | — |
| **代码部署、容器、镜像、日志** | ❌ | ✅ |
| **nginx 配置、.env 凭据、索引重建、夜间任务** | ❌ | ✅ |

---

## 1. 资源清单（2026-09-14 盘点）

| 资源 | 标识 | 规格/状态 | 用途 | 控制台入口 |
|---|---|---|---|---|
| 轻量应用服务器 | `121.41.4.12` | 2C2G，Alinux 3，Docker 26.1.3 | 跑 api / web / redis + 宿主 nginx | 轻量应用服务器 |
| RDS PostgreSQL | `pgm-bp1awp4nb8p7is1b` | PG 18.0，`pg.n1e.1c.1m`（1C1G 基础版）+ 20GB，**包年包月，2026-10-06 到期** | 唯一事实源（车系/款型/参数/销量/来源） | 云数据库 RDS |
| OSS | bucket `car-selection` | 杭州，**ColdArchive**，无生命周期规则，对象仅数月度榜单 HTML（各 ~330KB） | 抓取快照归档（应用只写不读） | 对象存储 OSS |
| RAM 用户 | `cloud_ali` | 只读策略（ReadOnlyAccess 等） | 运维巡检 CLI（**不能改资源**） | 访问控制 RAM |
| RAM 用户 | `car-oss-worker` | 自定义单桶策略 `carselection-oss-bucket`，1 把 AK | 生产 OSS 上传凭据（最小权限） | 访问控制 RAM |
| 域名 | `hp-car-selection-assistant.cn` | 万网注册，NS `dns23/24.hichina.com`；**ICP 已备案（2026-09）**，域名解析与 443 已上线 | 对外域名 | 域名 / 备案控制台 |
| Zilliz Cloud | collection `car_docs` | Serverless，12418 切片 | 稠密向量检索（**非阿里云**） | Zilliz Cloud 控制台 |
| DeepSeek / 模型服务 | — | API Key | LLM 与 embedding/rerank（**非阿里云**） | 各自平台控制台 |
| 邮箱 SMTP | smtp.163.com | 授权码 | 验证码邮件（**非阿里云**） | 163 邮箱 |
| 旧 ECS | `47.99.140.110` | **已释放** | 历史环境 | — |

---

## 2. 控制台运维详解

### 2.1 轻量应用服务器

| 操作 | 路径 | 说明与注意 |
|---|---|---|
| 重启 / 关机 / 启动 | 实例详情 → 右上角 | 重启后 Docker 服务自启，容器按 `restart: unless-stopped` 自动拉起；重启会中断正在跑的索引重建 |
| **防火墙** | 实例详情 → 防火墙 | 本项目需要：`22`（SSH）、`80`（HTTP）；**443 等备案通过后再开**。CLI 用的 RAM 用户是只读，**加规则必须来控制台** |
| 快照 | 实例详情 → 快照 / 自动快照策略 | 建议：自动快照每周一次 + **每次大变更前手动打一次**（快照只含系统盘，不含 RDS 数据） |
| 监控 | 实例详情 → 监控 | CPU / 内存 / 磁盘 / 公网流量；2C2G 机器上索引重建会把 CPU 打满属正常 |
| 远程连接 | 实例详情 → 远程连接（Workbench/VNC） | **SSH 不通时的救命通道**（改坏 iptables/sshd 时用） |
| 续费 / 升降配 | 实例详情 → 续费 / 变更配置 | 备案要求实例剩余时长充足，别让实例过期 |

### 2.2 RDS PostgreSQL（数据面，最需要小心）

| 操作 | 路径 | 说明与注意 |
|---|---|---|
| **续费** | RDS 控制台 → 实例 → 续费 | ⚠️ **当前到期日 2026-10-06**，过期会锁库（LockMode 变 Locked → 应用整体不可用，`/api/v1/ready` 会红） |
| 重置账号口令 / 新建账号 | 账号管理 | ⚠️ **顺序**：重置 → **立即**同步服务器 `/srv/carsel/backend/.env` 的 `DATABASE_URL` → `docker compose up -d --force-recreate api` → 跑 §5 验收。旧连接池会让 `/ready` 短暂仍绿（详见 §4） |
| 白名单 | 数据安全性 → 白名单 | 当前 8 条（生产 `121.41.4.12` + 若干办公/家庭 IP + `172.16.0.0/12`）。本机调试连不上时先看这里是否加了你的出口 IP |
| 外网地址 | 数据库连接 | 实例是内网类型，**已开外网地址**（应用用的是 `pgm-…bo…` 公网 endpoint）。轻量服务器与 RDS 不同 VPC，必须走外网 |
| 备份 / 恢复 | 备份恢复 | 自动全备保留 7 天 + 日志备份（可**按时间点恢复** PITR）。恢复演练：恢复到新实例 → 改 `.env` 指向新实例 → `--force-recreate api` → 验收 → 确认后再切回 |
| 监控与告警 | 监控与报警 | 建议配：连接数、CPU、磁盘使用率阈值告警 |
| 参数设置 | 参数设置 | 改参数需重启实例（会中断服务），非必要不动 |
| 日志管理 | 日志管理 | 慢日志 / 错误日志，排查数据库侧问题 |
| **删除保护** | 实例详情 → 更多 | ⚠️ **当前为关闭状态，建议开启**（防止误删库） |
| 维护窗口 | 实例详情 | 当前 `18:00Z–22:00Z`（北京 **02:00–06:00**）——与夜间任务 02:30 **重叠**，建议把维护窗口挪到 04:00–06:00 之外，或把夜间任务提前到 01:30 |

### 2.3 OSS

| 操作 | 路径 | 说明 |
|---|---|---|
| 用量监控 | OSS 控制台 → Bucket → 概览 | 存储量 / 流量 / 请求数 |
| 生命周期 | Bucket → 数据管理 → 生命周期 | 当前**无规则**；如需自动清理可加（如 365 天后删除或转低频）。对象都极小，成本可忽略 |
| 存储类型 | Bucket → 基础设置 | 当前 **ColdArchive（冷归档）**：写入便宜，**读取需先 restore（数小时）**；应用只写不读，故无影响 |
| 防盗链 / CORS | Bucket → 数据安全 | 前端不直连 OSS（图片走本站代理），无需配置 |
| 上传凭据 | RAM 控制台（非 OSS 控制台） | **OSS 控制台没有 AccessKey 管理入口**，这是常见误区 |

### 2.4 RAM（凭据）

| 操作 | 路径 | 说明 |
|---|---|---|
| 新建/禁用 AccessKey | RAM → 用户 → 认证管理 | 每个用户最多 2 把；轮换时「先建新 → 切生产 → 验证 → 删旧」 |
| 授权 | RAM → 权限管理 | `car-oss-worker` 挂自定义单桶策略（最小权限）；不要再给主账号 AK |
| 只读巡检 | `cloud_ali` | 本仓库 `.tools/aliyun/aliyun.exe` + `.tools/home/.aliyun/config.json` 用的就是它；**只读，改不了资源** |
| 换 OSS 凭据 | 建新 AK → 写本地 `.env` | 然后走 `.tools/env_merge_dburl.py` 同步服务器 + 重建容器（见 credential-rotation.md） |

### 2.5 域名与备案

| 操作 | 路径 | 说明 |
|---|---|---|
| 解析记录 | 云解析 DNS → 域名 → 解析设置 | 备案通过后添加 A 记录指向 `121.41.4.12`（`@` 与 `www`） |
| 实名认证 | 域名控制台 | `.cn` 必须实名；未实名/未过 serverHold 无法备案与解析 |
| **ICP 备案** | 阿里云备案控制台 | 前置：域名实名、账号实名、**轻量实例剩余时长 ≥3 个月**；期间网站不要对公网开放域名访问 |
| 证书（免费 DV） | 数字证书管理服务 | 备案通过后申请 → 配 nginx 443 + HSTS → 收窄 `CORS_ORIGINS` 为 https |
| 公安联网备案 | 全国互联网安全管理服务平台 | ICP 通过后 **30 天内**完成 |

### 2.6 费用与告警

| 操作 | 路径 | 说明 |
|---|---|---|
| 账单 / 续费管理 | 费用中心 | 轻量与 RDS 都是包年包月；**续费管理里开启到期提醒** |
| 预算与告警 | 费用中心 → 预算管理 | 设月度预算，超额邮件提醒 |
| 云监控告警 | 云监控 → 报警规则 | 建议：轻量 CPU>85% 持续 5 分钟、内存>90%、磁盘>85%；RDS 连接数/磁盘 |

---

## 3. 必须 SSH 的运维（速查命令）

```bash
# 进入服务器（管理入口已收紧到隧道，见 deployment.md §6）
ssh -i .deploy/ecs_key root@121.41.4.12

# 站点与依赖状态
curl -s localhost:8000/api/v1/ready          # {"status":"ready","checks":{"database":"ok","redis":"ok"}}
docker ps --format '{{.Names}} | {{.Status}}'
docker compose -f /srv/carsel/deploy/docker-compose.yml logs --tail 100 api

# 部署（只认 main；--force 立即部署当前 main）
/usr/local/bin/carsel-deploy.sh --check | --dry-run | --force

# 夜间任务（销量导入 + 索引重建）
/usr/local/bin/carsel-nightly.sh --force     # 数据回补后强制重建
tail -50 /srv/carsel/logs/sales-cron.log

# 向量索引
docker exec -w /srv/carsel/backend deploy-api-1 python tools/build_retrieval_index.py --target dense --smoke
curl -s -X POST -H "Authorization: Bearer $(cat /root/carsel-nightly-token.txt 2>/dev/null || cat /root/carsel-admin-token.txt)" \
  -H 'Content-Type: application/json' -d '{"target":"sparse"}' localhost:8000/api/v1/admin/rag/reindex

# 凭据体检（只读）
docker exec -w /srv/carsel/backend deploy-api-1 python tools/../deploy/verify_credentials.py   # 见 credential-rotation.md §4

# 管理审计（谁在什么时候做了什么）
tail -20 /srv/carsel/backend/.tmp/admin-audit.log

# ── Agent 会话记忆（生产存 Redis；本地无 REDIS_URL 时退化为进程内）──────────
# 键结构：agent:session:<sid>:profile（画像）/ :messages（对话历史，保留最近 50 条）/ :last（上次结果）
docker exec deploy-redis-1 redis-cli --scan --pattern 'agent:session:*'     # 有哪些会话
docker exec deploy-redis-1 redis-cli ttl agent:session:<sid>:profile        # 剩余 TTL（默认 3600s，每次访问续期）
# 单个会话重置（等价于前端「新对话」按钮 / POST /api/v1/agent/sessions/{sid}/reset）
docker exec deploy-redis-1 redis-cli del agent:session:<sid>:messages agent:session:<sid>:last
docker exec deploy-redis-1 redis-cli set agent:session:<sid>:profile '{}' ex 3600
# 清空全部会话记忆（谨慎：所有在线用户一起清）
docker exec deploy-redis-1 redis-cli --scan --pattern 'agent:session:*' | xargs -r docker exec -i deploy-redis-1 redis-cli del
```

---

## 4. 协同顺序与已知坑（都是踩过的）

1. **改 RDS 口令 ≠ 改完就完**：控制台重置后必须同步 `.env` + `--force-recreate api`，
   否则 api 崩溃循环；而**旧连接池会让 `/ready` 继续报绿**，掩盖故障（2026-09 实况）。
2. **只改本地 `.env` 不会生效**：生产读服务器 `.env`，不同步＝没改（实测三类模型 Key 全 401）。
3. **防火墙/端口**：控制台放行后才轮到 nginx `listen`；`443` 未备案不要对公网开。
4. **ColdArchive 只能 restore 后读**：需要在线读取时，先恢复或改存储类型。
5. **备份要演练**：RDS 恢复用「恢复到新实例」验证，不要直接覆盖生产实例。
6. **维护窗口与定时任务错开**：RDS 维护窗口内的闪断会让夜间任务失败（当前 02:00–06:00 vs 02:30）。
7. **管理入口只在隧道内可达**：公网 `/ops/` 与 `/api/v1/admin/` 一律 403（设计如此）。
8. **凭据 ≠ 身份**：多标签 token + 审计能回答「哪把凭据、什么时候、从哪个 IP 做了什么」，
   但凭据被转交后无法证明操作者本人；要「人」级别需短期会话/账号体系。
9. **Agent 画像在会话内累积**：某轮把约束理解错（例如把「不要奔驰」记成正向约束），后续每轮都会
   带着错误约束继续跑；排障或用户自助都要能重置——前端「新对话」按钮、`POST .../reset`
   （清记忆保留会话）、`DELETE .../{sid}`（删会话），或等 TTL（默认 1 小时）自然过期。

---

## 5. 巡检清单

| 频率 | 检查项 | 命令 / 入口 | 期望 |
|---|---|---|---|
| 每日 | 站点与依赖 | `curl -s localhost:8000/api/v1/ready` | `database: ok, redis: ok` |
| 每日 | 容器状态 | `docker ps` | api `healthy`、web/redis `Up` |
| 每日 | 夜间任务 | `tail -30 /srv/carsel/logs/sales-cron.log` | 导入成功或「已就绪」 |
| 每日 | 自动部署 | `carsel-deploy.sh --check` | 已是最新 / 有更新待部署 |
| 每周 | 磁盘与内存 | 控制台监控 或 `df -h` / `free -m` | 磁盘 <80% |
| 每周 | 管理审计 | `tail -50 .tmp/admin-audit.log` | 只有已知标签与 IP |
| 每月 | RDS 备份 | RDS 控制台 → 备份恢复 | 每日自动备份都在，保留 7 天 |
| 每月 | 索引水位 | `/ops/rag` 运行状态（隧道内） | `stale=false`，稠密/稀疏切片数一致 |
| 每季 | 凭据轮换 | credential-rotation.md | 按 90 天周期 |
| 到期前 | 续费 | 费用中心 / 续费管理 | 轻量、RDS、域名均不过期 |

---

## 6. 故障排查决策树（简表）

| 现象 | 先看 | 常见原因 |
|---|---|---|
| 站点打不开（公网） | 控制台防火墙 80 是否放行；nginx 是否在跑 | 端口未放行 / nginx 挂了 / 容器未启动 |
| 页面 200 但无数据 | `curl localhost:8000/api/v1/ready` | 数据库不可用（口令/白名单/实例锁定） |
| `/ready` 报 `database: down` | RDS 控制台实例状态、白名单、`.env` 口令 | 口令不同步 / 到期锁定 / 白名单缺出口 IP |
| 管理页面 403 | 是否从隧道访问 | 设计如此（公网一律拒绝） |
| 管理页面 401 | 凭据是否过期/被吊销 | 用了旧 token |
| Agent 回答里的销量是旧的 | `/ops/rag` 运行状态 → `stale` | 未重建索引（跑 `carsel-nightly.sh --force`） |
| 部署后无变化 | `carsel-deploy.sh --check`；`docker images` | main 未更新 / 构建失败（查 `/var/log/carsel-deploy.log`） |
| 服务器磁盘满 | 控制台监控 / `du -sh /srv/carsel/*` | 日志、快照、镜像堆积（`docker image prune -f`） |

---

## 7. 本轮盘点新增的待办（建议尽快处理）

| 优先级 | 事项 | 入口 |
|---|---|---|
| 高 | **RDS 续费**（2026-10-06 到期，锁定后全站不可用） | RDS 控制台 → 续费 |
| 高 | 开启 RDS **删除保护** | RDS 控制台 → 实例 → 更多 |
| 中 | 确认 RDS **自动备份**是否正常（最近一次备份为 2026-09-09） | RDS 控制台 → 备份恢复 |
| 中 | RDS **维护窗口**与夜间任务 02:30 错开 | RDS 控制台 → 实例详情 |
| 中 | 配置轻量**自动快照策略** + 云监控告警 | 轻量/云监控控制台 |
| 低 | OSS 生命周期规则（自动清理/转储） | OSS 控制台 → 生命周期 |
| 低 | 备案通过后：解析 → 证书 → nginx 443/HSTS → 收窄 CORS | 域名/证书控制台 + SSH |
