# 凭据轮换运行手册（Credential Rotation Runbook）

## 1. 为什么需要轮换

2026-09-13 的安全评审发现：仓库根目录此前**没有 `.dockerignore`**，`backend/.env` 落在 Docker
构建上下文内（`docker build` 会把整个上下文送给 daemon，命中缓存的层还会留存副本）。
按凭据管理规范，凡曾进入构建上下文的密钥一律**视为已暴露**，应当轮换。

| 凭据 | 环境变量 | 状态 |
|---|---|---|
| 管理后台 Bearer | `ADMIN_API_TOKEN` | ✅ 已轮换（32 字节 CSPRNG，服务器 `/root/carsel-admin-token.txt`，600） |
| 部署 SSH 密钥对 | `.deploy/ecs_key` | ✅ 已重新生成并只在本地保留 |
| GitHub 访问令牌 | —（未入库） | ✅ 已轮换 |
| RDS 数据库口令 | `DATABASE_URL` | ⬜ 待轮换 |
| DeepSeek API Key | `DEEPSEEK_API_KEY` | ⬜ 待轮换 |
| 嵌入模型 Key | `EMBEDDING_API_KEY` | ⬜ 待轮换 |
| 重排模型 Key | `RERANK_API_KEY` | ⬜ 待轮换 |
| Zilliz / Milvus Token | `MILVUS_TOKEN` | ⬜ 待轮换 |
| OSS RAM AK/SK | `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` | ⬜ 待轮换 |
| SMTP 授权码 | `SMTP_PASSWORD` | ⬜ 待轮换 |

`.dockerignore` 已阻断后续泄漏路径；上表待办属于「历史暴露」的收尾。

## 2. 通用原则（顺序不能颠倒）

1. **先建新 → 再切换 → 最后废旧**。任何时刻都保留一份可用凭据，避免服务中断；
   反过来（先删旧）会出现无法回滚的窗口。
2. **只改 `.env` 不等于轮换完成**：旧凭据只要还能用，就等于没换。必须回控制台禁用/删除。
3. **密钥不进仓库、不进聊天、不进截图**。仓库只留 `.env.example` 占位符（`.gitignore` +
   `.dockerignore` 双重拦截，扫描器 `reviewer/scan_secrets.py` 兜底）。
4. **轮换后验证业务面**，而不是只看进程是否活着（见 §4 清单）。
5. **留痕**：记录「谁 / 何时 / 哪个凭据 / 验证方式 / 旧凭据何时失效」，下次审计直接对照。

## 3. 单个凭据的轮换步骤（模板）

以「RDS 数据库口令」为例，其余凭据只是控制台入口不同：

```bash
# ① 控制台新建凭据（RDS：账号管理 → 重置密码；OSS：RAM → AccessKey；模型：各自控制台新建 Key）
#    生成 32 位以上随机值，例如：openssl rand -base64 32

# ② 更新本地 .env（.env 已 gitignore，不会入库）
#    DATABASE_URL=postgresql+psycopg://<user>:<new-pass>@<host>:5432/<db>

# ③ 同步到服务器（用 scp 传文件，不要用 ssh 'cat >' —— Windows 侧的管道会破坏 UTF-8 与行尾）
scp -i .deploy/ecs_key backend/.env root@<server>:/srv/carsel/backend/.env
ssh -i .deploy/ecs_key root@<server> 'chmod 600 /srv/carsel/backend/.env'

# ④ 重建容器：env_file 只在「创建容器」时读取，restart 不会重新加载
ssh -i .deploy/ecs_key root@<server> 'cd /srv/carsel/deploy && docker compose up -d --force-recreate api'

# ⑤ 按 §4 验证；通过后回控制台禁用/删除旧凭据
```

### 各凭据的控制台入口与注意点

| 凭据 | 控制台入口 | 注意点 |
|---|---|---|
| RDS 口令 | 阿里云 RDS → 账号管理 → 重置密码 | 重置立即生效（新连接即失败），建议先建一个应用专用账号（最小权限）再切换，实现零停机；同时核查白名单 IP 与连接审计 |
| OSS AK/SK | RAM 控制台 → 用户 → AccessKey | 建议**不再用主账号 AK**：新建 RAM 子账号，只授权单个 bucket 前缀的读写；禁用旧 AK 后确认图片上传/代理仍正常 |
| DeepSeek Key | platform.deepseek.com → API Keys | 新建 Key → 切换 → 删除旧 Key；删除后旧 Key 立即失效 |
| 嵌入 / 重排 Key | 对应模型平台（百炼 / 方舟等）控制台 | 换 Key 不影响已入库向量（同一模型与维度时），无需重建索引；换**模型**才需要重建 |
| Zilliz Token | Zilliz Cloud → API Keys | 新建 Token → 切换 → 删除旧 Token；确认稠密召回仍命中（`/api/v1/ready` 只探 DB/Redis，不覆盖向量库） |
| SMTP 授权码 | 邮箱服务商（QQ / 163 / 企业邮）→ 生成授权码 | 旧授权码作废；切换后走「发送验证码」接口实测一封 |

> Zilliz 连接问题排查提示：本机 `psql`/TCP 通不代表后端能连（内网端点与公网端点不同）。
> 若 `DATABASE_URL` 指向内网端点而服务器不在同一 VPC，会表现为 TCP 可达但 `ConnectionTimeout`。

## 4. 轮换后的验证清单

```bash
# 依赖数据库 + Redis
curl -s localhost:8000/api/v1/ready
# → {"status":"ready","checks":{"database":"ok","redis":"ok"}}

# 依赖 LLM（Agent 回答）
curl -s -X POST localhost:8000/api/v1/agent/sessions -H 'Content-Type: application/json' -d '{}'

# 依赖向量库 + 嵌入 + 重排（RAG 检索）
curl -s 'localhost:8000/api/v1/rag/status'    # 管理凭据保护，需带 Authorization 头

# 管理凭据本身（应 200；不带 token 应 401；未配置 token 应 503）
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  localhost:8000/api/v1/admin/imports

# SMTP（发送验证码）
curl -s -X POST localhost:8000/api/v1/auth/email-code -H 'Content-Type: application/json' \
  -d '{"email":"<你的邮箱>"}'
```

全部通过后：`docker compose build --no-cache api web`（清掉可能含旧密钥的构建层），
再 `docker image prune -f`。

## 5. 轮换周期与后续改进

- **周期**：常规 90 天一轮；人员变动、疑似泄漏、凭据误提交时立即轮换。
- **权限收敛（待办）**：OSS 换 RAM 子账号最小权限；RDS 拆「应用账号 / 运维账号」；
  管理 token 支持多凭据与过期时间；`.env` 逐步迁移到 KMS / 密钥托管（当前决策为暂缓）。
- **自动化（可选）**：把 §3 的 ②③④ 固化成脚本（输入新值 → 写 `.env` → scp → 重建 → 跑 §4 验证），
  减少手工步骤；注意脚本本身不得回显凭据。
