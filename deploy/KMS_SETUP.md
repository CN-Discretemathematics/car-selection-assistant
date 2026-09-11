# 生产密钥注入：阿里云 KMS 凭据管家 + ECS 实例 RAM 角色

## 架构

```
阿里云 KMS 凭据管家（carsel/prod/env，text 型通用凭据）
        ↑ GetSecretValue（STS 临时凭证，自动轮换）
ECS 实例 RAM 角色（KMSreadonly）→ 容器 entrypoint（deploy/fetch_secrets.py）
        ↓ 注入 os.environ（不进镜像/仓库，运行时仅存在于进程内存）
应用进程（pydantic-settings：环境变量优先于 .env ✓ 已就绪）
```

## 当前状态（2026-09-11）

| 步骤 | 状态 |
| --- | --- |
| 创建 RAM 角色 KMSreadonly + KMSReadOnlyAccess | ✅ 已完成（主账号） |
| ECS 实例绑定 RAM 角色 | ⏳ 待主账号控制台 |
| 创建 KMS 凭据 carsel/prod/env | ⏳ 待主账号控制台（payload 已备好，见下） |
| 应用侧（entrypoint/测试/文档） | ✅ 已完成 |

> 权限说明：`KMSReadOnlyAccess` 为托管策略，授予**所有**凭据的读权限——单凭据
> 场景可用；更小权限可换自定义策略仅授 `kms:GetSecretValue`（限 carsel/prod/env）。

## 剩余两步（主账号控制台）

### 1. 绑定实例 RAM 角色

ECS 控制台 → 实例 `i-bp14zlrr7sq0upv8xv5s` → 操作「授予/收回 RAM 角色」→ 选择
`KMSreadonly`。（CLI `aliyun ecs AttachInstanceRamRole` 需要 RAM 子用户具备
`ecs:AttachInstanceRamRole` 权限，当前子用户被拒。）

### 2. 创建 KMS 凭据

KMS 控制台 → 凭据管家 → 创建凭据：
- 凭据名称：`carsel/prod/env`
- 凭据值：服务器 `/srv/carsel/backend/.tmp/kms-payload.json` 的内容
  （已从生产 .env 生成，权限 600；**粘贴后删除该文件**）
- 类型：通用凭据（默认 text）

### 3. 启用（服务器，凭据与绑定完成后）

```bash
sed -i 's/^# \(KMS_ROLE\|KMS_SECRET_NAME\|KMS_REGION\|KMS_SECRET_FORMAT\|KMS_FAIL_OPEN\)=/\1=/' /srv/carsel/backend/.env
grep -n "^KMS_" /srv/carsel/backend/.env
cd /srv/carsel/deploy && docker compose up -d api   # 重启即走 KMS 注入
```

验证：`docker logs deploy-api-1 2>&1 | grep fetch-secrets`（应看到「已注入 N 个密钥」），
随后跑一轮完整回归。

## 应用侧实现（已完成）

- `deploy/fetch_secrets.py`：容器 ENTRYPOINT——实例元数据 STS → RPC 签名调
  KMS GetSecretValue（text 按原文 / binary 解 base64）→ 注入 os.environ → exec 应用；
  拉取失败默认阻断启动（KMS_FAIL_OPEN=1 可降级）；部分配置（只设其一）响亮失败。
- 应用零改动：pydantic-settings 环境变量优先于 .env（标准行为）✓。
- 测试：deploy 路径 13 项（签名/解码/main 调度/fail-closed/部分配置）。

## 轮换与回滚

- 轮换：KMS 凭据管家放入新版本 → 重启容器生效。
- 回滚：控制台恢复上一版本 → 重启。
- 注意：宿主机 .env 的密钥明文在启用 KMS 后应清空（§3 前），
  compose `env_file` 注入的空值由 KMS 注入在启动时补全。
