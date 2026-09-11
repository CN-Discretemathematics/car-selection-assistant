"""fetch_secrets 单元测试（签名确定性 / 凭据解析 / 编码，纯函数无网络）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

import fetch_secrets  # noqa: E402


def test_rpc_sign_deterministic():
    """签名确定性：同输入同输出，且为合法 base64(HMAC-SHA1)。"""
    params = {
        "AccessKeyId": "STS.test",
        "Action": "GetSecretValue",
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": "nonce-1",
        "SignatureVersion": "1.0",
        "Timestamp": "2026-09-11T04:00:00Z",
        "Version": "2016-01-20",
        "SecretName": "carsel/prod/env",
    }
    sig1 = fetch_secrets._rpc_sign(params, "secret&")
    sig2 = fetch_secrets._rpc_sign(dict(reversed(list(params.items()))), "secret&")
    assert sig1 == sig2, "参数顺序不影响签名（sorted）"
    raw = base64.b64decode(sig1)
    assert len(raw) == 20, "HMAC-SHA1 摘要长度"


def test_rpc_sign_matches_reference():
    """与独立实现的 HMAC-SHA1 对照（防签名串构造回归）。注意：RPC 规范要求
    签名密钥 = AccessKeySecret + '&'，测试参照用同一约定。"""
    import urllib.parse

    params = {"Action": "GetSecretValue", "Timestamp": "2026-01-01T00:00:00Z", "Version": "2016-01-20"}
    query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(params.items())
    )
    string_to_sign = "GET&%2F&" + urllib.parse.quote(query, safe="-_.~")
    expect = base64.b64encode(
        hmac.new(b"secret&&", string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    assert fetch_secrets._rpc_sign(params, "secret&") == expect


def test_parse_secret_data_json_and_dotenv():
    js = fetch_secrets._parse_secret_data('{"DATABASE_URL": "x", "DEEPSEEK_API_KEY": "y"}', "json")
    assert js == {"DATABASE_URL": "x", "DEEPSEEK_API_KEY": "y"}
    dotenv = fetch_secrets._parse_secret_data("# 注释\nA=1\nB = two words\n", "dotenv")
    assert dotenv == {"A": "1", "B": "two words"}


def test_secret_data_base64_roundtrip():
    """KMS SecretData 为 base64(JSON)：GetSecretValue 返回后需先解码。"""
    payload = {"ADMIN_API_TOKEN": "t" * 16}
    raw = base64.b64encode(__import__("json").dumps(payload).encode()).decode()
    assert fetch_secrets._parse_secret_data(base64.b64decode(raw).decode(), "json") == payload
