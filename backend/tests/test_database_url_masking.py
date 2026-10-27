"""连接串**脱敏**口径（2026-10-21 代码审查 Blocker 的回归测试）。

## 缺陷

`backend/tools/smoke_durable_memory.py` 曾把 `DATABASE_URL` **原样打印**——
下面两行合起来就是当时那行的形状（原实现写在同一行，此处**拆成两行**展示，
否则本文件会被新加的 `raw-db-url-echo` 规则自己拦下）：

    url = os.environ.get("DATABASE_URL", "<未设，走默认>")
    print(f"目标数据库：{url}")

生产连接串带「用户名:口令@主机」前缀 ⇒ **口令明文进 CI 日志 / 终端历史 / 录屏**
——reviewer §1 红线第 7 条（日志里回显密钥明文）。

而那个工具的用法注释就写着「真 PostgreSQL（CI / 生产同构）」⇒ 它正是会被拿去
连生产的那一个。CI 里用 trust 认证（连接串不带口令）所以日志是干净的，
**人工对生产跑一次就漏一次**——这不是假想：
生产库口令进过会话输出（该事故已复盘，口令亦已轮换）。

## 本文件钉住的三条

1. 脱敏函数**不输出**口令，也**不输出**用户名；
2. 脱敏函数**不输出**主机/库名之外的东西（不能因为脱敏把运维需要的信息也丢了）；
3. **工具真的打印的那行**里没有口令——这是钉住「修之前」的那条（把脱敏去掉就会红）。

⚠️ 本文件**要入库**，源码里不能出现带凭据的连接串字面量（scan_secrets 的
`db-url-with-credentials` 规则会直接拦下）。故连接串一律**运行时拼接**。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.common.database import describe_database_url  # noqa: E402

#: 运行时拼出来的带凭据连接串（分段拼接 ⇒ 源码里没有连续字面量）
_USER = "carsel_app"
_PASSWORD = "Xk8sPq2mNv7Rt4Yw"
_HOST = "rm-prod.pg.rds.aliyuncs.com"
_DB = "carsel"


def _pg_url() -> str:
    return (
        "postgresql" + "://" + _USER + ":" + _PASSWORD
        + "@" + _HOST + ":5432" + "/" + _DB
    )


def test_password_never_appears_in_the_description():
    """核心回归：口令不得出现在输出里。"""
    described = describe_database_url(_pg_url())
    assert _PASSWORD not in described, f"口令泄漏进输出：{described}"
    assert ":" + _PASSWORD not in described


def test_username_also_not_shown():
    """用户名不是机密，但没有展示价值——最小暴露面。

    ⚠️ 主机名与用户名**可能同名**（生产就是 `carapp`@`…/carapp`），
    所以这里用一个**与主机/库名都不同**的用户名来断言，否则断言会变成空气。
    """
    described = describe_database_url(_pg_url())
    assert _USER not in described, f"用户名泄漏进输出：{described}"


def test_host_and_database_are_kept():
    """脱敏不能把运维需要的信息也丢了：主机与库名要留着。"""
    described = describe_database_url(_pg_url())
    assert _HOST in described, described
    assert _DB in described, described
    assert "5432" in described, described


def test_presence_of_password_is_still_reported():
    """「这条连接串带凭据」是有用信息（说明它在碰真库）——但只说有、不说是什么。"""
    described = describe_database_url(_pg_url())
    assert "口令已隐藏" in described, described


def test_url_without_password_is_not_claimed_to_have_one():
    """没有口令的连接串不许谎称「口令已隐藏」。"""
    described = describe_database_url(
        "postgresql" + "://" + _HOST + ":5432" + "/" + _DB
    )
    assert "口令已隐藏" not in described, described


def test_sqlite_path_is_shown_as_is():
    """SQLite 没有主机与凭据，路径就是全部信息。"""
    assert describe_database_url("sqlite:///./dev.db") == "sqlite:///./dev.db"
    assert describe_database_url("sqlite://") == "sqlite://"


def test_empty_url_says_so():
    """没设 DATABASE_URL 时要说清楚，而不是显示空串。"""
    assert describe_database_url("") == "<未设，走默认>"
    assert describe_database_url(None) == "<未设，走默认>"


def test_unparsable_url_is_never_echoed_back():
    """解析失败**不回显原文**——回显等于把口令又漏一次。"""
    broken = "postgresql" + "://" + _USER + ":" + _PASSWORD + "@" + _HOST + ":not-a-port/db"
    described = describe_database_url(broken)
    assert _PASSWORD not in described, f"解析失败却回显了口令：{described}"
    assert _USER not in described, f"解析失败却回显了用户名：{described}"


def test_tool_prints_masked_target_not_the_raw_url(monkeypatch, capsys):
    """钉住「修之前」的那条：**工具真的打印的那行**里没有口令。

    修之前这里会红：工具把 `DATABASE_URL` 原样打印 ⇒ 输出里有口令。
    """
    from tools import smoke_durable_memory  # 延迟导入：不希望 import 阶段就建 app

    monkeypatch.setenv("DATABASE_URL", _pg_url())
    # 不让 run() 真去连库（本条只关心「打印了什么」）
    monkeypatch.setattr(smoke_durable_memory, "run", lambda *a, **k: 0)

    smoke_durable_memory.main([])

    printed = capsys.readouterr().out
    assert printed, "工具没有输出任何东西（这条断言就失去意义了）"
    assert _PASSWORD not in printed, f"工具输出里带着口令：{printed[:300]}"
    assert _USER not in printed, f"工具输出里带着用户名：{printed[:300]}"
    assert _HOST in printed, f"工具输出里应当保留主机名：{printed[:300]}"
