# -*- coding: utf-8 -*-
"""部署脚本的**共享目录权限**契约（2026-10-23）

## 为什么有这条契约

宿主 `backend/.tmp` 被 compose bind-mount 进 api 容器，管理审计日志、dense 构建水位标记、
销量导入 flag 都落在这个目录。**容器里那个身份写不进去时，不会有任何接口报错**
（审计写入已吞 `OSError`），后果是两个静默退化：

1. 管理审计日志不再落盘 —— 只剩 docker logs 里那一份，等于审计链断了一半；
2. **dense 构建水位标记写不进去** ⇒ 状态页永远判「向量集合落后」
   ⇒ 夜间任务每晚都白全量重建一次（5 分钟 CPU + embedding 费用）。

## 判据（钉住**为什么这么写**，而不只是「有这段代码」）

1. **两种身份都得能写**：容器内 uid 10001，以及宿主 root（夜间任务在宿主上跑，
   以 root 读/删 flag、读水位标记）。
2. **`cap_drop: ALL` ⇒ 权限位是唯一依据**：连以 root 跑��容器都没有 DAC_OVERRIDE，
   所以「uid 0 就无所不能」在这里是**错的**——必须用权限位覆盖两种身份。
3. **目录 1777 而非 777**：sticky 位保证「谁都能写，但只能删自己的文件」，
   避免一个身份误删另一个身份的文件。
4. **每次部署都重新放开**：新建文件的权限由 umask 决定 ⇒ 光 chmod 目录不够，
   现有文件也要逐个放开，否则老文件仍然写不进去。
5. **chown 在 chmod 之后**：chown 可能清掉 setuid/setgid 位，顺序反了权限位会被抹掉。
"""
from __future__ import annotations

import re
from pathlib import Path

DEPLOY_SH = Path(__file__).resolve().parents[2] / "deploy" / "carsel-deploy.sh"


def _text() -> str:
    return DEPLOY_SH.read_text(encoding="utf-8")


def _perm_block(text: str) -> str:
    """切流前的共享目录授权片段。"""
    start = text.index("backend/.tmp")
    # 往前退到这段注释块的起点
    head = text.rindex("\n#", 0, start)
    end = text.index("docker compose up -d", start)
    return text[head:end]


def test_deploy_script_authorizes_shared_tmp_dir():
    text = _text()
    assert "backend/.tmp" in text, "部署脚本没有处理共享目录 backend/.tmp"
    block = _perm_block(text)
    assert 'chmod 1777 "$APP_DIR/backend/.tmp"' in block, (
        "共享目录必须是 1777（sticky：谁能写、只能删自己的文件）"
    )


def test_shared_tmp_dir_existing_files_are_reopened():
    """**现有文件**也要逐个放开：新建文件的权限由 umask 决定，
    光 chmod 目录对已存在的文件无效（老文件仍然写不进去）。"""
    block = _perm_block(_text())
    assert "find" in block and "chmod 666" in block, (
        "现有文件必须逐个 chmod 666（否则 umask 造成的老文件仍然写不进去）"
    )
    assert "-maxdepth 1" in block, "只放开本层文件，不要递归进子目录"


def test_shared_tmp_dir_is_owned_by_container_uid():
    """属主必须是 api 容器的运行 uid（deploy/backend.Dockerfile 里是 10001），
    否则「非 root 容器」写不进去。"""
    block = _perm_block(_text())
    assert "chown -R 10001:10001" in block, "共享目录属主必须是容器的运行 uid 10001"


def test_chown_comes_after_chmod():
    """**顺序**：chown 在 chmod 之后。

    chown 可能清掉 setuid/setgid 位 ⇒ 顺序反了，刚放开的权限位会被抹掉。
    """
    block = _perm_block(_text())
    i_chmod = block.index("chmod 1777")
    i_chown = block.index("chown -R")
    assert i_chmod < i_chown, f"顺序反了：chmod@{i_chmod} 应在 chown@{i_chown} 之前"


def test_authorization_runs_before_container_start():
    """授权必须在 `docker compose up -d` **之前**——容器一起来就可能写审计日志。"""
    text = _text()
    i_perm = text.index('chmod 1777 "$APP_DIR/backend/.tmp"')
    i_up = text.index("docker compose up -d")
    assert i_perm < i_up, "共享目录授权必须在容器启动之前"


def test_authorization_does_not_hard_fail_deploy():
    """授权失败**不得**中断部署：`.tmp` 写不进去的代价是「审计与水位标记退化」，
    不是「站点起不来」⇒ 用 `|| true` 兜住，让部署继续（问题会在断言步骤暴露）。"""
    block = _perm_block(_text())
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("chmod ") or s.startswith("chown "):
            assert "|| true" in s, f"授权命令必须容错（|| true），否则会中断部署：{s}"


def test_docker_compose_cap_drop_all_means_permission_bits_are_the_only_means():
    """回归守卫：compose 里的 `cap_drop: ALL` 是「权限位是唯一依据」的前提。

    如果哪天有人把 cap_drop 去掉（给容器加回 DAC_OVERRIDE），这段注释的前提就变了，
    应当重新评估共享目录的权限策略 —— 所以把它钉进测试里，让人必须回来看一眼。
    """
    compose = (
        Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert "cap_drop" in compose and "ALL" in compose, (
        "compose 的 cap_drop: ALL 变了 ⇒ 共享目录权限策略要重新评估"
    )
    block = _perm_block(_text())
    assert "cap_drop" in block, "部署脚本里应说明「权限位是唯一依据」的原因"


def test_shared_tmp_dir_is_excluded_from_rsync():
    """`backend/.tmp` 必须被 rsync 排除：它是运行期数据，
    同步过去会**覆盖掉**刚刚授权好的权限位。"""
    text = _text()
    assert re.search(r"--exclude 'backend/\.tmp/'", text), (
        "rsync 必须排除 backend/.tmp/（否则会覆盖掉刚授权的权限）"
    )
