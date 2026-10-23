# -*- coding: utf-8 -*-
"""部署脚本的**非 root 运行时断言**契约（2026-10-24 补）

## 为什么有这条契约

容器改为非 root 运行（uid 10001，见 `deploy/backend.Dockerfile`）是安全加固的
「用户身份」那一层。但 Dockerfile 里写 `USER carsel` 只是**声明**——
真正要保证的是**跑起来时**确实是那个身份：

- compose 里任何一处 `user:` 覆盖，声明就失效；
- 镜像构建漏了 USER 指令（改 Dockerfile 时很容易漏），声明就失效；
- 基础镜像换版本时默认用户变了，声明也可能失效。

⇒ `deploy/tests/verify_non_root.sh` 在部署后当场断言一次，失败即回滚。
「声明 + 运行时断言」两层都在，才叫护栏；只有声明那叫许愿。

## 为什么需要这条**测试**（审计发现的覆盖缺口）

`verify_non_root.sh` 是 **shell 脚本**，`backend/tests/` 下**没有任何 pytest 覆盖它**
⇒ 它可以被人改坏（删掉断言、把 `uid = 0` 的判断去掉…）而 CI 一声不吭。

本文件把它的**判据**钉进 pytest：脚本的**逻辑本身**必须仍然能挡住 root。

## 判据

1. **取不到 uid ⇒ 失败**（不是跳过）——「查不到」不是「通过」；
2. **uid == 0 ⇒ 失败**（这是要拦的那件事）；
3. **非 0 ⇒ 通过**；
4. 部署脚本**必须调用它**，且失败**必须回滚**（不许只记个日志就继续）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

VERIFY_SH = Path(__file__).resolve().parents[2] / "deploy" / "tests" / "verify_non_root.sh"
DEPLOY_SH = Path(__file__).resolve().parents[2] / "deploy" / "carsel-deploy.sh"


def _script() -> str:
    return VERIFY_SH.read_text(encoding="utf-8")


def test_verify_script_exists():
    assert VERIFY_SH.is_file(), f"非 root 断言脚本不存在：{VERIFY_SH}"


def test_root_uid_is_a_failure():
    """uid == 0（root）必须判为失败。"""
    body = _script()
    assert '"$uid" = "0"' in body or '"$uid" = 0' in body, (
        "必须显式判 uid == 0 为失败（这是这道护栏要拦的那件事）"
    )
    # 失败必须置位（fail=1），不是只打个日志
    idx = body.index('"$uid" = "0"')
    tail = body[idx:idx + 300]
    assert "fail=1" in tail, (
        "uid == 0 的分支必须 `fail=1`（只 echo 不置位 ⇒ 退出码仍是 0 ⇒ 护栏失效）"
    )


def test_unreadable_uid_is_a_failure():
    """**取不到 uid ⇒ 失败**，不是跳过。

    为什么：脚本存在「容器没跑 / exec 失败 / 没有 id 命令」的情形。
    若那种情形被判成通过 ⇒ 容器起不来时护栏反而放行 ⇒ 护栏在最需要时失效。
    """
    body = _script()
    assert re.search(r"if\s*\[\s*-z\s+\"\$uid\"\s*\];", body), (
        "必须显式判「取不到 uid」（容器未运行 / exec 失败）"
    )
    idx = body.index('-z "$uid"')
    # ⚠️ 窗口要够大：变异可能把 `fail=1` 换成 `continue`（跳过这一轮），
    #    那样 fail 标志根本没被置位 ⇒ 退出码仍是 0 ⇒ 护栏在最需要时失效。
    #    只看**该分支自己**（到下一个 `fi` 为止）——脚本里别处也有 `continue`。
    branch = body[idx:body.index("\n  fi", idx)]
    assert "fail=1" in branch, (
        "取不到 uid 必须 `fail=1`（否则护栏在容器没起来时反而放行）"
    )
    # `continue` 本身**不是**问题（原脚本就有：跳过本轮，但 fail 已置位）
    # ⇒ 判据只看 fail 标志，不看 continue。


def test_nonzero_uid_passes():
    """非 0 uid 必须算通过（不能把正常容器也判成失败）。"""
    body = _script()
    assert "非 root" in body, "非 root 的分支应当报「非 root」"
    assert re.search(r'else\s*\n\s*echo[^\n]*非 root', body), "非 0 uid 必须走通过分支"


def test_script_exits_with_fail_flag():
    """退出码必须来自 `fail` 标志，而不是恒定 0。"""
    body = _script()
    assert re.search(r"exit\s+\"\$fail\"", body), "退出码必须是 `exit \"$fail\"`"


def test_deploy_script_calls_the_assertion_and_rolls_back():
    """部署脚本必须**调用**它，且失败**回滚**（不许只记日志就继续）。"""
    deploy = DEPLOY_SH.read_text(encoding="utf-8")
    assert "verify_non_root.sh" in deploy, "部署脚本没有调用非 root 断言"
    call = deploy.index("verify_non_root.sh")
    tail = deploy[call:call + 900]
    assert "deploy-api:rollback" in tail, "非 root 断言失败必须回滚到上一版镜像"
    assert "exit 1" in tail, "非 root 断言失败必须以非零退出"


def test_compose_declares_non_root_user():
    """compose 里**不得**有 `user:` 覆盖——那会让 Dockerfile 的 USER 声明失效。

    （`docker-compose.yml` 里若出现 `user:`，`USER carsel` 就不作数了。）
    """
    compose = (
        Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert not re.search(r"^\s*user:", compose, re.MULTILINE), (
        "compose 里出现 `user:` 会覆盖 Dockerfile 的 USER 声明 ⇒ 非 root 断言会失败"
    )


def test_script_syntax_is_valid():
    """脚本本身语法得对（否则部署时直接报错，反倒什么都拦不住）。

    ⚠️ Windows 上 `shutil.which("bash")` 可能命中 **WSL 转发器**
    （`C:\\Windows\\system32\\bash.EXE`）——它**存在但根本跑不起来**
    （无 WSL 时报 "execvpe(/bin/bash) failed"）⇒ 光探测「存在」不够，
    必须**真跑一次**看它能不能用。
    跑不起来的 bash ⇒ 跳过（CI 的 Ubuntu runner 上有真 bash，会真的检查）。
    """
    import shutil

    bash = shutil.which("bash")
    if not bash:
        import pytest

        pytest.skip("本机没有 bash（Windows 无 WSL）⇒ 语法检查由 CI 的 Ubuntu runner 执行")
    probe = subprocess.run(
        [bash, "-n", str(VERIFY_SH)], capture_output=True, text=True, encoding="utf-8"
    )
    if "execvpe" in (probe.stderr or "") or "WSL" in (probe.stderr or ""):
        import pytest

        pytest.skip(f"本机的 bash 不可用（WSL 转发器）⇒ 语法检查由 CI 执行：{probe.stderr[:120]}")
    assert probe.returncode == 0, f"verify_non_root.sh 语法错误：{probe.stderr[:300]}"
