# -*- coding: utf-8 -*-
"""部署脚本的**预热步骤**契约（2026-10-23）

## 为什么有这条契约

容器重建后，**新进程**里的稀疏索引（BM25，14032 chunk）是空的 ⇒ 由**第一个用户请求**
同步重建（实测 5.4 秒）⇒ 那一笔请求的端到端从 ~4s 变成 ~10s。

`deploy/carsel-deploy.sh` 因此在切流后主动打一次检索（`warmup_retrieval`），
把这笔钱花在部署窗口里。

## 判据（钉住**为什么这么写**，而不只是「有这段代码」）

1. **必须打在运行中的 api 进程上**（`127.0.0.1:8000`）——
   另起进程重建的索引会随那个进程退出而丢弃（生产模式不会按数据量自动重建）。
2. **不许调 LLM**：预热走 RAG「试运行」端点，只跑检索链；打 agent 消息端点会
   调 LLM（花钱 + 多等几秒）。
3. **预热失败 ⇒ 回滚**：检索链都不通，这次部署不算成功。
4. **顺序**：健康检查 → 预热 → 记录成功状态（预热必须打在已经活过来的新进程上）。
"""
from __future__ import annotations

from pathlib import Path

DEPLOY_SH = Path(__file__).resolve().parents[2] / "deploy" / "carsel-deploy.sh"


def _text() -> str:
    return DEPLOY_SH.read_text(encoding="utf-8")


def _warmup_block(text: str) -> str:
    """预热函数 + 调用点所在的片段。"""
    start = text.index("warmup_retrieval() {")
    end = text.index("# 日志自截断", start)
    return text[start:end]


def test_deploy_script_has_warmup_step():
    text = _text()
    assert "warmup_retrieval" in text, "部署脚本里没有预热步骤"
    assert "WARMUP_URL=" in text and "WARMUP_QUERY=" in text


def test_warmup_hits_the_running_api_process():
    """必须打**运行中的** api 进程（另起进程建的索引会随进程退出而丢弃）。"""
    text = _text()
    assert "http://127.0.0.1:8000/api/v1/admin/rag/query" in text, (
        "预热必须打在运行中的 api 进程（127.0.0.1:8000）"
    )
    # 不许用「另起进程跑 reindex」那种会丢索引的方式
    # ⇒ 查**全文**而不是只查函数块：另起进程可能写在任何位置
    assert "build_retrieval_index.py" not in text, (
        "预热不得另起进程重建索引（索引会随那个进程退出而丢弃）"
    )
    assert "python -m" not in _warmup_block(text)


def test_warmup_does_not_call_the_llm():
    """预热只跑检索链 ⇒ 不打 agent 消息端点（那会调 LLM：花钱 + 更慢）。"""
    text = _text()
    block = _warmup_block(text)
    assert "/agent/sessions" not in block, "预热不得打 agent 消息端点（会调 LLM）"
    # 端点定义在函数外的变量区 ⇒ 这里查全文
    assert "/admin/rag/query" in text, "预热端点必须是 RAG 试运行（只跑检索链）"


def test_warmup_failure_rolls_back():
    """检索链都不通 ⇒ 这次部署不算成功 ⇒ 回滚。"""
    text = _text()
    call = text.index("if ! warmup_retrieval; then")
    block = text[call:call + 900]
    assert "deploy-api:rollback" in block, "预热失败必须回滚到上一版镜像"
    assert "exit 1" in block, "预热失败必须以非零退出"


def test_warmup_unavailable_token_does_not_block_deploy():
    """**admin token 过期/无权限** ⇒ 跳过预热、**不阻断部署**。

    为什么必须这样：预热是**可选优化**，若它自己有故障却能卡死所有部署，
    那个可选优化就变成了阻断项 —— token 一过期，站点就再也发不了新版。
    """
    block = _warmup_block(_text())
    assert "401|403|404" in block, "token 不可用（401/403）或端点缺失（404）必须跳过而非阻断"
    # 跳过分支必须 return 0（成功），不能 return 1
    skip_at = block.index("401|403|404")
    after = block[skip_at:skip_at + 400]
    assert "return 0" in after, "跳过分支必须 return 0（不阻断部署）"
    assert "return 1" not in after.split("hits=")[0], "跳过分支不得 return 1"


def test_warmup_tries_next_token_instead_of_giving_up():
    """第一个 token 不可用（401/403）⇒ 必须**试下一个**，而不是直接放弃。

    服务器上同时存在 nightly 与 admin 两个 token 文件；只试第一个的话，
    恰好那个过期了就会白白跳过预热（本来另一个是好的）。
    """
    block = _warmup_block(_text())
    assert "carsel-nightly-token.txt" in block and "carsel-admin-token.txt" in block
    # 401/403/404 必须是 continue（换下一个），不是 break
    assert "continue ;;" in block, "token 不可用时必须 continue（试下一个）"
    assert "break; fi" not in block, "不可用时不得 break（那会放弃后面的 token）"


def test_warmup_runs_after_health_check_and_before_success_record():
    """顺序：健康检查 → 预热 → 记录成功状态。"""
    text = _text()
    i_health = text.index("healthy=0")
    i_warm = text.index("if ! warmup_retrieval; then")
    i_record = text.index("last-deploy.json")
    assert i_health < i_warm < i_record, (
        f"顺序不对：health={i_health} warmup={i_warm} record={i_record}"
    )
