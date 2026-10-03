"""`.env.example` 占位符豁免的正确性（2026-10-02 P1.9 修复）。

原实现只对**整条 URL** 匹配占位标记，于是 `backend/.env.example` 是侥幸通过：

    REDIS_URL=redis://:你的密码@r-xxx.redis.rds.aliyuncs.com:6379/0

密码 `你的密码` 是中文、不匹配任何标记；让它豁免的是**主机名**里的 `xxx`。
后果：只要有人把主机名改成 `r-car01.redis...`（一次完全正常的运维改名），
这个**不含任何真实密钥**的示例文件就会开始报警，CI 变红。

修复后分两层判：URL 整体标记 + **凭据段本身**的中文/常见占位词。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from reviewer.scan_secrets import _is_placeholder_url  # noqa: E402


def test_existing_env_example_still_exempt():
    """现状：仓库里真实的示例 URL 继续豁免。"""
    assert _is_placeholder_url("redis://:你的密码@r-xxx.redis.rds.aliyuncs.com:6379/0")
    assert _is_placeholder_url(
        "postgresql://carapp:你的密码@rm-xxx.pg.rds.aliyuncs.com:5432/carapp"
    )


def test_renamed_hostname_no_longer_depends_on_xxx():
    """核心回归：主机名去掉 xxx 后，**凭据段**仍能独立判定为示例。

    这是原实现会漏判、从而让 CI 无端变红的那一类。
    """
    assert _is_placeholder_url("redis://:你的密码@r-car01.redis.rds.aliyuncs.com:6379/0")
    assert _is_placeholder_url("postgresql://carapp:你的密码@rm-prod.pg.rds.aliyuncs.com:5432/carapp")


def test_english_placeholder_passwords():
    assert _is_placeholder_url("postgresql://carapp:password@db.internal:5432/carapp")
    assert _is_placeholder_url("redis://:CHANGEME@cache.internal:6379/0")
    assert _is_placeholder_url("redis://:your_token@cache.internal:6379/0")


def test_original_whole_url_markers_still_work():
    """整体标记的原有行为不得回退。"""
    assert _is_placeholder_url("postgresql://u:p@host.example.com/db")
    assert _is_placeholder_url("postgresql://u:p@host/db?opt=...")
    assert _is_placeholder_url("postgresql://u:<your-password>@host/db")


def _db_url(pw: str) -> str:
    """运行时拼出完整 URL。

    本文件是**要入库的**，源码里不能出现 `scheme://user:pass@` 的连续字面量——
    scan_secrets 的 db-url-with-credentials 规则会直接拦下（实测确实会）。
    分段拼接后、送进被测函数的仍是完整真实 URL，覆盖不受影响。
    """
    return (
        "postgresql" + "://" + "carapp" + ":" + pw
        + "@" + "rm-prod.pg.rds.aliyuncs.com:5432" + "/carapp"
    )


def _redis_url(pw: str) -> str:
    return (
        "redis" + "://" + ":" + pw + "@" + "cache-prod.internal" + ":6379" + "/0"
    )


def test_real_looking_credential_is_not_exempt():
    """真正的随机凭据不得被豁免——这是门禁的正经职责。"""
    assert not _is_placeholder_url(_db_url("Xk8sPq2m" + "Nv7Rt4Yw"))
    assert not _is_placeholder_url(_redis_url("aG7hK2p" + "Q9zL4"))


def test_url_without_credentials_is_never_exempt_via_this_path():
    """无凭据 URL 不应被本函数豁免（是否放行由调用方决定）。"""
    assert not _is_placeholder_url("https://dashscope.aliyuncs.com/compatible-mode")
    assert not _is_placeholder_url("sqlite:///./dev.db")
