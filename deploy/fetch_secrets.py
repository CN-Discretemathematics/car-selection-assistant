"""启动时从阿里云 KMS 凭据管家拉取密钥并注入环境变量，然后 exec 应用进程。

用途（生产密钥注入，替代明文 .env）：
- 容器 entrypoint 调用本脚本；脚本从 ECS 实例 RAM 角色的元数据端点获取 STS 临时
  凭证（无长期 AK、自动轮换），按阿里云 RPC 签名规范调 KMS GetSecretValue，
  解密后把密钥键值注入 os.environ，最后 os.execvp 替换为应用进程。
- 密钥不落盘：注入仅发生在内存中；实例 RAM 角色由云平台管理，宿主机/镜像零凭据。

环境变量：
- KMS_ROLE           实例绑定的 RAM 角色名（必填，缺失时跳过拉取直接 exec）
- KMS_SECRET_NAME    KMS 凭据名称（如 carsel/prod/env）
- KMS_REGION         KMS 地域（默认 cn-hangzhou）
- KMS_SECRET_FORMAT  凭据格式：json（默认，平铺键值）| dotenv（KEY=VALUE 行）
- APP_CMD            拉取后要 exec 的命令（默认继承 CMD）

用法（Dockerfile ENTRYPOINT）：
    ENTRYPOINT ["python", "/srv/carsel/fetch_secrets.py"]
    CMD ["python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

METADATA_BASE = "http://100.100.100.200/latest/meta-data"
KMS_ENDPOINT = "https://kms.cn-hangzhou.aliyuncs.com"
KMS_API_VERSION = "2016-01-20"


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
    """从 ECS 实例元数据获取 RAM 角色的 STS 临时凭证（无需任何长期 AK）。"""
    url = f"{METADATA_BASE}/ram/security-credentials/{role_name}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    if data.get("Code") != "Success":
        raise RuntimeError(f"STS 凭证获取失败：{data.get('Code')}")
    return data


def _kms_get_secret_value(sts: dict, secret_name: str, region: str) -> str:
    """调 KMS GetSecretValue，返回凭据明文（SecretData，base64 已解码）。"""
    params = {
        "AccessKeyId": sts["AccessKeyId"],
        "Action": "GetSecretValue",
        "Format": "JSON",
        "SecretName": secret_name,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2016-01-20",
    }
    if sts.get("SecurityToken"):
        params["SecurityToken"] = sts["SecurityToken"]
    params["Signature"] = _rpc_sign(params, sts["AccessKeySecret"])
    url = f"{KMS_ENDPOINT}/?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return base64.b64decode(data["SecretData"]).decode()


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
    # Docker ENTRYPOINT+CMD 模式：CMD 数组作为独立参数传进来，需重新拼接
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""

    if role and secret_name:
        try:
            count = inject_secrets(role, secret_name, region, fmt)
            print(f"[fetch-secrets] 已注入 {count} 个密钥（{secret_name}）", file=sys.stderr)
        except Exception as err:  # noqa: BLE001 - 拉取失败原则上应阻断启动
            print(f"[fetch-secrets] 密钥拉取失败：{type(err).__name__}: {err}", file=sys.stderr)
            if os.environ.get("KMS_FAIL_OPEN", "") != "1":
                sys.exit(1)  # 生产默认 fail-closed：密钥拉不到就不启动
            print("[fetch-secrets] KMS_FAIL_OPEN=1，降级为现有环境变量继续启动", file=sys.stderr)

    if cmd:
        os.execvp("sh", ["sh", "-c", cmd])
    os.execvp("sh", ["sh"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
