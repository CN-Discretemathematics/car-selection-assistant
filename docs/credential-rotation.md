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
| RDS 数据库口令 | `DATABASE_URL` | ✅ 已轮换并同步生产（2026-09-13，见 §5） |
| DeepSeek API Key | `DEEPSEEK_API_KEY` | ✅ 已轮换并同步生产 |
| 嵌入模型 Key | `EMBEDDING_API_KEY` | ✅ 已轮换并同步生产 |
| 重排模型 Key | `RERANK_API_KEY` | ✅ 已轮换并同步生产（与嵌入同值） |
| OSS RAM AK/SK | `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` | ✅ 新 AK 已在生产生效；⬜ 旧 AK 待控制台禁用 |
| Zilliz / Milvus Token | `MILVUS_TOKEN` | ⬜ 未轮换（现网有效，`car_docs` 集合可 describe） |
| SMTP 授权码 | `SMTP_PASSWORD` | ⬜ 未轮换（现网有效，登录实测通过） |

`.dockerignore` 已阻断后续泄漏路径；上表未完成项属于「历史暴露」的收尾。

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

# ② 先在本地 .env 更新并用应用自己的客户端验通（不要拿未验证的值去动生产）
cd backend && PYTHONPATH=vendor:. python ../deploy/verify_credentials.py

# ③ 外科式同步到服务器：只替换白名单键，DATABASE_URL 只替换口令部分
#    —— 不要整文件 scp！本地与生产的环境相关值不同（RDS 内网/公网端点、REDIS_URL、
#       CORS_ORIGINS、ADMIN_API_TOKEN 等），整文件覆盖会把生产配置改坏
scp -i .deploy/ecs_key backend/.env root@<server>:/tmp/local.env   # 只作为取值来源
ssh -i .deploy/ecs_key root@<server> 'chmod 600 /tmp/local.env'
#    在服务器上用一次性脚本做「白名单键 + DATABASE_URL 口令」合并（先 cp -p 备份、chmod 600），
#    合并完成后立刻 rm -f /tmp/local.env
#    传文件用 scp，不要用 ssh 'cat >' —— Windows 侧的管道会破坏 UTF-8 与行尾

# ④ 重建容器：env_file 只在「创建容器」时读取，restart 不会重新加载
ssh -i .deploy/ecs_key root@<server> 'cd /srv/carsel/deploy && docker compose up -d --force-recreate api'

# ⑤ 按 §4 验证（必须包含业务面：/ready 可能被连接池里的旧连接"救活"）；通过后回控制台禁用/删除旧凭据
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

**首选：用仓库自带的只读校验脚本**（复用应用自身的客户端，逐个打通六类凭据，只打印状态不打印密钥）：

```bash
# 在 api 容器内运行（注意：docker exec 不会把工作目录加进 sys.path，必须显式给 PYTHONPATH）
docker cp deploy/verify_credentials.py deploy-api-1:/tmp/
docker exec -e PYTHONPATH=/srv/carsel/backend -w /srv/carsel/backend \
    deploy-api-1 python /tmp/verify_credentials.py
# 期望：deepseek / embedding / zilliz / rerank / oss / smtp 全 OK，退出码 0
```

**补充：接口与业务面**

```bash
# 依赖数据库 + Redis（注意：见 §5——它可能被连接池里的旧连接"救活"，不能只信它）
curl -s localhost:8000/api/v1/ready
# → {"status":"ready","checks":{"database":"ok","redis":"ok"}}

# 依赖 LLM + 嵌入 + 向量库 + 重排的真实业务路径（管理凭据保护）
curl -s -X POST -H "Authorization: Bearer $ADMIN_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"比亚迪海豚的续航里程是多少","top_k":3}' \
  localhost:8000/api/v1/admin/rag/query

# 依赖 LLM 的 Agent 回答
sid=$(curl -s -X POST localhost:8000/api/v1/agent/sessions | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"message":"预算15万，家用5口人，想要新能源SUV"}' \
  "localhost:8000/api/v1/agent/sessions/$sid/messages"

# 管理凭据本身（应 200；不带 token 应 401；未配置 token 应 503）
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  localhost:8000/api/v1/admin/brands

# SMTP（发送验证码）
curl -s -X POST localhost:8000/api/v1/auth/email-code -H 'Content-Type: application/json' \
  -d '{"email":"<你的邮箱>"}'
```

全部通过后：`docker compose build --no-cache api web`（清掉可能含旧密钥的构建层），
再 `docker image prune -f`。

## 5. 实战教训（2026-09-13 首次轮换）

1. **只改本地 `.env` 不算轮换**。本次只在本地更新了模型类 Key 与 RDS 口令，生产过程仍在用旧值：
   DeepSeek / 嵌入 / 重排全部 401（LLM 会**静默降级**为确定性回答，所以「页面还能打开」不等于凭据有效），
   RDS 口令不同步会让 api 直接崩溃循环。
2. **`/ready` 会说谎**。RDS 口令变更后近一小时 `/ready` 一直是 `database: ok`——连接池里的既有连接
   在改口令后仍然可用。`docker compose up -d --force-recreate api` 换上新进程才暴露
   `FATAL: password authentication failed`。**结论：轮换后必须重建容器，并跑 §4 的业务面校验。**
3. **新建连接才算验证**。`docker exec <container> python script.py` 不会把容器工作目录加进 `sys.path`
   （uvicorn 用 `python -m` 才自带 cwd），脚本放 `/tmp` 时要显式 `-e PYTHONPATH=<WORKDIR>`。
4. **对比差异用指纹，不肉眼看值**。核对方是否已是新值：`sha256(值)[:12]` + 长度 + 掩码形状
   （字母数字→`x`、保留标点），既能确认「两边是否一致 / 口令是否为同一个」，又不会把明文带进聊天或工单。
5. **同步要外科式**。只替换白名单键；`DATABASE_URL` 只替换口令部分，host / 库名 / 参数保持服务器原值
   （本地与服务器的内网、公网端点常常不同）。改前 `cp -p .env .env.bak-$(date +%Y%m%d-%H%M%S)` + `chmod 600`。
6. **先在本地验通新凭据再推生产**（本次本地六项全绿后才动服务器，避免把无效值推上去）。
7. **切换即时生效**：模型类 Key 若已在控制台删除，旧值立刻 401——这正是发现「生产未同步」的信号；
   反之 OSS 旧 AK 未禁用时新旧都能用，**别忘了回控制台禁用旧 AK**（§1 表最后一项）。

## 6. 轮换周期与后续改进

- **周期**：常规 90 天一轮；人员变动、疑似泄漏、凭据误提交时立即轮换。
- **权限收敛（待办）**：OSS 换 RAM 子账号最小权限；RDS 拆「应用账号 / 运维账号」；
  管理 token 支持多凭据与过期时间；`.env` 逐步迁移到 KMS / 密钥托管（当前决策为暂缓）。
- **配置待清理**：生产 `CORS_ORIGINS` 仍指向已释放的旧 ECS（`http://47.99.140.110:8080`）。
  当前前端经 nginx 同源代理 `/api`，不走 CORS 所以无影响，但应在备案域名确定后更新为真实来源。
- **自动化（可选）**：把 §3 的 ②③④ 固化成脚本（输入新值 → 写 `.env` → scp → 重建 → 跑 §4 验证），
  减少手工步骤；注意脚本本身不得回显凭据（`deploy/verify_credentials.py` 已是 §4 的验收侧）。
