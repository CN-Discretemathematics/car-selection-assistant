"""启动时从阿里云 KMS 凭据管家拉取密钥并注入环境变量，然后 exec 应用进程。

用途（生产密钥注入，替代明文 .env）：
- 容器 entrypoint 调用本脚本；脚本从 ECS 实例 RAM 角色的元数据端点获取 STS 临时
  凭证（无长期 AK、自动轮换），按阿里云 RPC 签名规范调 KMS GetSecretValue，
  解密后把密钥键值注入 os.environ，最后 exec 应用进程。
- 密钥不进镜像/仓库；配合 deploy/KMS_SETUP.md 的 .env 清理步骤，宿主机不留明文。

环境变量：
- KMS_ROLE           实例绑定的 RAM 角色名（与 KMS_SECRET_NAME 必须同时设置；
                     本地开发两者都不设，直接 exec 跳过拉取）
- KMS_SECRET_NAME    KMS 凭据名称（如 carsel/prod/env；text 型通用凭据）
- KMS_REGION         KMS 地域（默认 cn-hangzhou，决定 endpoint）
- KMS_SECRET_FORMAT  凭据格式：json（默认，平铺键值）| dotenv（KEY=VALUE 行）
- KMS_FAIL_OPEN      非 1 时密钥拉取失败即阻断启动（fail-closed，生产推荐）
- APP_CMD            拉取后要 exec 的命令（默认继承 CMD）

用法（Dockerfile ENTRYPOINT）：
    ENTRYPOINT ["python", "/srv/carsel/fetch_secrets.py"]
    CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

METADATA_BASE = "http://100.100.100.200/latest/meta-data"
KMS_API_VERSION = "2016-01-20"
_ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REGION_RE = re.compile(r"^[a-z0-9-]+$")


def _kms_endpoint(region: str) -> str:
    if not _REGION_RE.fullmatch(region or ""):
        raise ValueError(f"非法 KMS_REGION：{region!r}")
    return f"https://kms.{region}.aliyuncs.com"


def _popencode(s: str) -> str:
    """阿里云 RPC 签名的 percentEncoding（RFC3986：-_.~ 不编码，其余全编码）。"""
    return urllib.parse.quote(s, safe="-_.~")


def _rpc_sign(params: dict[str, str], access_key_secret: str) -> str:
    """阿里云 RPC 签名：HMAC-SHA1(SecretKey+'&', 'GET&%2F&<sorted-query>')。"""
    query = "&".join(
        f"{_popencode(k)}={_popencode(v)}" for k, v in sorted(params.items())
    )
    string_to_sign = f"GET&{_popencode('/')}&{_popencode(query)}"
    digest = hmac.new(
        (access_key_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode()


def _sts_credentials(role_name: str) -> dict:
    """从 ECS 实例元数据获取 RAM 角色的 STS 临时凭证（无需任何长期 AK）。

    评审 S6：角色名进入 URL path，先做白名单校验（防路径改写/注入）。
    """
    if not _ROLE_NAME_RE.fullmatch(role_name or ""):
        raise ValueError(f"非法 KMS_ROLE：{role_name!r}")
    url = f"{METADATA_BASE}/ram/security-credentials/{urllib.parse.quote(role_name, safe='')}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    if data.get("Code") != "Success":
        raise RuntimeError(f"STS 凭证获取失败：{data.get('Code')}")
    return data


def _kms_get_secret_value(sts: dict, secret_name: str, region: str) -> str:
    """调 KMS GetSecretValue，返回凭据明文（SecretData 按 SecretDataType 解码）。

    评审 S2：text 型通用凭据的 SecretData **按原文返回**（不是 base64）——只有
    binary 型才是 base64；无条件 b64decode 会让文档路径直接 crash-loop。
    """
    endpoint = _kms_endpoint(region)  # 评审 S3：region 必须真正决定 endpoint
    params = {
        "AccessKeyId": sts["AccessKeyId"],
        "Action": "GetSecretValue",
        "Format": "JSON",
        "SecretName": secret_name,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": KMS_API_VERSION,
    }
    if sts.get("SecurityToken"):
        params["SecurityToken"] = sts["SecurityToken"]
    params["Signature"] = _rpc_sign(params, sts["AccessKeySecret"])
    url = f"{endpoint}/?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return _decode_secret_payload(data)


def _decode_secret_payload(data: dict) -> str:
    """GetSecretValue 响应 → 凭据明文（text 按原文；binary 先 base64 解码）。"""
    raw = data.get("SecretData") or ""
    if data.get("SecretDataType") == "binary":
        return base64.b64decode(raw).decode()
    return raw


def _parse_secret_data(raw: str, fmt: str) -> dict[str, str]:
    """凭据明文 → 键值字典（json 平铺 或 dotenv 行两种格式）。"""
    if fmt == "json":
        data = json.loads(raw)
        return {k: str(v) for k, v in data.items()}
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def inject_secrets(role: str, secret_name: str, region: str, fmt: str) -> int:
    """拉取凭据并注入 os.environ（KMS 值覆盖镜像/compose 的同名变量）。返回注入键数。"""
    sts = _sts_credentials(role)
    raw = _kms_get_secret_value(sts, secret_name, region)
    kv = _parse_secret_data(raw, fmt)
    for key, value in kv.items():
        os.environ[key] = value
    return len(kv)


def main() -> int:
    role = os.environ.get("KMS_ROLE", "")
    secret_name = os.environ.get("KMS_SECRET_NAME", "")
    region = os.environ.get("KMS_REGION", "cn-hangzhou")
    fmt = os.environ.get("KMS_SECRET_FORMAT", "json")

    # 评审 S4：部分配置（只设其一）必须响亮失败——静默跳过会让生产退化为宿主机
    # 残留的（可能过期的）明文 env，且无任何信号
    if bool(role) != bool(secret_name):
        print("[fetch-secrets] 配置不完整：KMS_ROLE 与 KMS_SECRET_NAME 必须同时设置", file=sys.stderr)
        return 1

    # Docker exec-form CMD 以独立 argv 传入（评审 S1 BLOCKER：
    # ["sh","-c","<cmd>"] 不能 join 展平——POSIX sh -c 语义会把首个词当命令串，
    # 导致 `alembic upgrade head` 静默不执行）；保留 argv 向量原样 exec。
    argv = sys.argv[1:]

    if role and secret_name:
        try:
            count = inject_secrets(role, secret_name, region, fmt)
            print(f"[fetch-secrets] 已注入 {count} 个密钥（{secret_name}）", file=sys.stderr)
        except Exception as err:  # noqa: BLE001 - 拉取失败原则上应阻断启动
            print(f"[fetch-secrets] 密钥拉取失败：{type(err).__name__}: {err}", file=sys.stderr)
            if os.environ.get("KMS_FAIL_OPEN", "") != "1":
                return 1  # 生产默认 fail-closed：密钥拉不到就不启动
            print("[fetch-secrets] KMS_FAIL_OPEN=1，降级为现有环境变量继续启动", file=sys.stderr)

    if not argv:
        # 评审 S7：空 CMD 的静默 sh 会以 stdin EOF 立即退出形成重启循环——响亮失败
        print("[fetch-secrets] 未提供启动命令（CMD 为空）", file=sys.stderr)
        return 1
    if argv[:2] == ["sh", "-c"] and len(argv) >= 3:
        os.execvp("sh", ["sh", "-c", argv[2]])
    os.execvp(argv[0], argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
