"""fetch_secrets 单元测试（签名确定性 / 凭据解码 / main 调度与 fail-closed，无真实网络）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

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
    """与独立实现的 HMAC-SHA1 对照（防签名串构造回归）。

    注意：RPC 规范的签名密钥 = AccessKeySecret + '&'（本例 'secret&' → 密钥 'secret&&'）。
    """
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


def test_decode_secret_payload_text_vs_binary():
    """评审 S2 BLOCKER 回归：text 型凭据 SecretData 按原文返回（不是 base64）；
    只有 binary 型需要解码。"""
    payload = {"ADMIN_API_TOKEN": "t" * 16}
    raw_text = json.dumps(payload)
    assert fetch_secrets._decode_secret_payload(
        {"SecretData": raw_text, "SecretDataType": "text"}
    ) == raw_text
    raw_b64 = base64.b64encode(raw_text.encode()).decode()
    assert fetch_secrets._decode_secret_payload(
        {"SecretData": raw_b64, "SecretDataType": "binary"}
    ) == raw_text


def test_kms_endpoint_uses_region():
    """评审 S3：region 必须决定 endpoint（此前硬编码 cn-hangzhou）。"""
    assert fetch_secrets._kms_endpoint("cn-shanghai") == "https://kms.cn-shanghai.aliyuncs.com"
    with pytest.raises(ValueError):
        fetch_secrets._kms_endpoint("../evil")


def test_sts_credentials_rejects_bad_role(monkeypatch):
    """评审 S6：角色名白名单校验（防元数据 URL path 注入）。"""
    with pytest.raises(ValueError):
        fetch_secrets._sts_credentials("../evil?x=1")


def _main(monkeypatch, argv, env, inject=None):
    """运行 main() 并捕获 execvp 调用；返回 (exit_code, execvp_args)。"""
    captured = {}

    def fake_execvp(cmd, args):
        captured["cmd"] = cmd
        captured["args"] = args

    monkeypatch.setattr(fetch_secrets.os, "execvp", fake_execvp)
    monkeypatch.setattr(fetch_secrets.sys, "argv", ["fetch_secrets.py", *argv])
    monkeypatch.setattr(fetch_secrets.os, "environ", dict(fetch_secrets.os.environ, **env))
    if inject is None:
        monkeypatch.setattr(
            fetch_secrets, "inject_secrets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用"))
        )
    else:
        monkeypatch.setattr(fetch_secrets, "inject_secrets", inject)
    code = fetch_secrets.main()
    return code, captured


def test_main_dispatches_sh_c_vector(monkeypatch):
    """评审 S1 BLOCKER 回归：exec-form CMD ["sh","-c",cmd] 必须原样 exec，
    不得展平（展平会让 alembic 迁移静默不执行）。"""
    cmd = "python -m alembic upgrade head && python -m uvicorn app.main:app"
    code, captured = _main(
        monkeypatch,
        ["sh", "-c", cmd],
        {"KMS_ROLE": "", "KMS_SECRET_NAME": ""},
    )
    assert code == 0
    assert captured == {"cmd": "sh", "args": ["sh", "-c", cmd]}


def test_main_dispatches_plain_vector(monkeypatch):
    code, captured = _main(
        monkeypatch,
        ["python", "-m", "uvicorn"],
        {"KMS_ROLE": "", "KMS_SECRET_NAME": ""},
    )
    assert code == 0
    assert captured == {"cmd": "python", "args": ["python", "-m", "uvicorn"]}


def test_main_empty_cmd_fails_loud(monkeypatch):
    """评审 S7：空 CMD 响亮失败（静默 sh 会形成重启循环）。"""
    code, captured = _main(monkeypatch, [], {"KMS_ROLE": "", "KMS_SECRET_NAME": ""})
    assert code == 1
    assert "args" not in captured


def test_main_partial_kms_config_fails_closed(monkeypatch):
    """评审 S4：只配 KMS_ROLE 不配 KMS_SECRET_NAME → 响亮失败，不静默跳过。"""
    code, _ = _main(monkeypatch, ["sh", "-c", "true"], {"KMS_ROLE": "r", "KMS_SECRET_NAME": ""})
    assert code == 1


def test_main_fail_closed_on_fetch_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kms down")

    code, _ = _main(
        monkeypatch,
        ["sh", "-c", "true"],
        {"KMS_ROLE": "r", "KMS_SECRET_NAME": "s"},
        inject=boom,
    )
    assert code == 1, "fail-closed：拉取失败必须阻断启动"


def test_main_fail_open_when_opted_in(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kms down")

    code, captured = _main(
        monkeypatch,
        ["sh", "-c", "true"],
        {"KMS_ROLE": "r", "KMS_SECRET_NAME": "s", "KMS_FAIL_OPEN": "1"},
        inject=boom,
    )
    assert code == 0
    assert captured == {"cmd": "sh", "args": ["sh", "-c", "true"]}


def test_main_injects_before_exec(monkeypatch):
    """注入成功后 exec 应用；注入的键值进入 os.environ。"""
    seen = {}

    def fake_inject(role, name, region, fmt):
        seen["call"] = (role, name, region, fmt)
        fetch_secrets.os.environ["SECRET_X"] = "1"
        return 1

    code, _ = _main(
        monkeypatch,
        ["sh", "-c", "true"],
        {"KMS_ROLE": "carsel-ecs-role", "KMS_SECRET_NAME": "carsel/prod/env",
         "KMS_REGION": "cn-hangzhou", "KMS_SECRET_FORMAT": "json"},
        inject=fake_inject,
    )
    assert code == 0
    assert seen["call"] == ("carsel-ecs-role", "carsel/prod/env", "cn-hangzhou", "json")
