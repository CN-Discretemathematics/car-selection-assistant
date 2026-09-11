# 生产密钥注入：阿里云 KMS 凭据管家 + ECS 实例 RAM 角色

## 架构

```
阿里云 KMS 凭据管家（carsel/prod/env）
        ↑ GetSecretValue（STS 临时凭证，自动轮换）
ECS 实例 RAM 角色（carsel-ecs-role）→ 容器 entrypoint（deploy/fetch_secrets.py）
        ↓ 注入 os.environ（不落盘、不写文件）
应用进程（pydantic-settings：环境变量优先于 .env ✓ 已就绪）
```

## 一次性云资源操作（主账号控制台或 CLI）

### 1. 创建 RAM 角色并授权（仅限该凭据的 GetSecretValue）

```bash
aliyun ram CreateRole \
  --RoleName carsel-ecs-role \
  --AssumeRolePolicyDocument '{"Statement":[{"Action":"sts:AssumeRole","Effect":"Allow","Principal":{"Service":["ecs.aliyuncs.com"]}}],"Version":"1"}'

aliyun ram CreatePolicy \
  --PolicyName carsel-kms-read \
  --PolicyDocument '{"Statement":[{"Action":["kms:GetSecretValue","kms:Decrypt"],"Effect":"Allow","Resource":["acs:kms:cn-hangzhou:1788486296964680:secret/carsel/prod/env"]}],"Version":"1"}'

aliyun ram AttachPolicyToRole --PolicyType Custom --PolicyName carsel-kms-read --RoleName carsel-ecs-role
```

### 2. 创建 KMS 凭据（值 = 生产密钥 JSON 平铺键值）

```bash
aliyun kms CreateSecret \
  --RegionId cn-hangzhou \
  --SecretName carsel/prod/env \
  --SecretData "$(python -c '
import json
keys = ["DATABASE_URL","DEEPSEEK_API_KEY","MILVUS_URI","MILVUS_TOKEN",
        "EMBEDDING_API_KEY","RERANK_API_KEY","REDIS_URL","OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_SECRET","SMTP_PASSWORD","ADMIN_API_TOKEN"]
env = {}
for line in open("backend/.env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        if k.strip() in keys:
            env[k.strip()] = v.strip()
print(json.dumps(env))
')" \
  --VersionId v1
```

### 3. ECS 实例绑定 RAM 角色

```bash
aliyun ecs AttachInstanceRamRole \
  --RegionId cn-hangzhou \
  --InstanceIds '["i-bp14zlrr7sq0upv8xv5s"]' \
  --RamRoleName carsel-ecs-role
```

### 4. 服务器 .env 加 KMS 配置

```bash
cat >> /srv/carsel/backend/.env <<'EOF'
KMS_ROLE=carsel-ecs-role
KMS_SECRET_NAME=carsel/prod/env
KMS_REGION=cn-hangzhou
KMS_SECRET_FORMAT=json
KMS_FAIL_OPEN=0
EOF
cd /srv/carsel/deploy && docker compose up -d api
```

## 应用侧实现（已完成）

- `deploy/fetch_secrets.py`：容器 ENTRYPOINT——从实例元数据（100.100.100.200）获取
  STS 临时凭证 → RPC 签名调 KMS GetSecretValue → 注入 os.environ → `os.execvp` exec
  应用。密钥不落盘；拉取失败默认阻断启动（KMS_FAIL_OPEN=1 可降级）。
- 应用零改动：pydantic-settings 环境变量优先于 .env（标准行为）✓。

## 轮换与回滚

- 轮换：KMS 凭据管家放入新版本（VersionId v2）→ 重启容器生效。
- 回滚：KMS 控制台恢复上一版本 → 重启。
- 封禁排查：SSH 被断时检查 安全中心 → 防暴力破解 IP 封禁列表。
