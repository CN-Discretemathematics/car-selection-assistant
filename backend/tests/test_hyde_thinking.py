"""HyDE 的 LLM 调用必须关闭思考链（`thinking="disabled"`）。

## 为什么要钉这一条

HyDE 的提示词是「50 字内、只含车系名与参数键值、**不要解释、不要列表**」——
一个**窄任务**。而 `deepseek-flash` 端点默认 `enabled + effort=high`
（`llm.py` 的 `_chat_payload` 注释援引官方文档），即窄任务在跑深度思考。
更糟的是 `_hyde_text` 用 `asyncio.run` **同步阻塞**在检索流水线里。

真机对拍（8 条真实购车问句）：

| | 不传（改前） | 传 disabled |
| --- | --- | --- |
| 响应带 `reasoning_content` | **8/8** | 0/8 |
| 延迟中位 | **5308ms** | **798ms** |

⚠️ **输出逐字一致 0/8**——「thinking 只影响延迟不改输出」**不成立**。

## 本改动对生产的现实影响：零

`RETRIEVAL_HYDE` 默认关（见 `HYDE_ENABLED`），该路径今天不执行。
且它 2026-09-09 已做过 A/B（E24/E25），六项指标与基线逐位相同、**无增益故被否决**。
本测试存在的意义是：**将来谁打开 `RETRIEVAL_HYDE`，都不会再撞上 5 秒同步阻塞。**
"""
from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import app.common.llm as llm_mod
from app.rag import pipeline


class _RecordingLLM:
    """记录调用参数的假客户端；顺带模拟「思考链关闭后不再返回 reasoning_content」。"""

    available = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, msgs, tools=None, temperature=0.2, json_mode=False, thinking=None):
        self.calls.append({"temperature": temperature, "thinking": thinking, "n_msgs": len(msgs)})
        return {"choices": [{"message": {"content": "汉 轴距：2920mm；唐 轴距：2820mm。"}}]}


def test_hyde_call_disables_thinking(monkeypatch):
    """行为断言：实际发出去的调用带 `thinking="disabled"`。"""
    fake = _RecordingLLM()
    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **k: fake)

    text = pipeline._hyde_text({"query": "帮我对比一下汉和唐"})

    assert fake.calls, "HyDE 根本没发起调用 —— 断言可能因错误理由而绿"
    assert fake.calls[0]["thinking"] == "disabled", (
        f"HyDE 没关思考链，实际传的是 {fake.calls[0]['thinking']!r}。"
        "窄任务跑深度思考会让同步阻塞的流水线多花约 4.5 秒。"
    )
    assert text, "关闭思考后必须仍能取到助写文本"


def test_hyde_source_pins_the_kwarg():
    """结构断言：`client.chat(...)` 这个调用**在语法上**必须带 thinking 关键字。

    为什么不用「源码里搜字符串」：`inspect.getsource` 会把 docstring 一起返回，
    而本函数的 docstring 里就写着那个参数的说明——用字符串搜，删掉参数也会绿
    （这正是反向验证实测出来的假绿灯）。故改用 AST，只认**调用实参**。
    """
    src = textwrap.dedent(inspect.getsource(pipeline._hyde_text))
    fn = ast.parse(src).body[0]
    assert isinstance(fn, ast.FunctionDef)

    chat_calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "chat"
    ]
    assert chat_calls, "源码里找不到 client.chat(...) 调用 —— 测试可能已失效"
    # 不写死 [0]：ast.walk 是广度优先，将来函数里多出一个 .chat（如重试）
    # 会让「只查第一个」悄悄钉错对象。对**全部**调用断言。
    for call in chat_calls:
        kwarg_values = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
        assert "thinking" in kwarg_values, (
            "某个 client.chat() 调用没传 thinking —— 窄任务又跑回深度思考了，"
            "同步阻塞的流水线会多花约 4.5 秒"
        )
        assert isinstance(kwarg_values["thinking"], ast.Constant) and (
            kwarg_values["thinking"].value == "disabled"
        ), "thinking 传的不是常量 \"disabled\"，可能被改成了按端点默认跑"


def test_hyde_is_off_by_default():
    """钉住「本改动对当前生产零影响」这个前提，别让人误以为它在跑流量。

    **必须用子进程**：本函数第一版在进程内 `importlib.reload(app.retrieval.config)`，
    而该模块顶层有 `load_dotenv()` 且在导入时就把环境变量固化成模块全局——
    原地 reload 会把这些值**永久**改写给本进程后续所有用例。实测污染了
    `test_router.py` 的 8 个用例（一起跑 8 failed，单独跑 48 passed）。

    若将来有人把 `RETRIEVAL_HYDE` 默认打开，本测试会红，
    那时**必须**补一次 HyDE 开启态的检索 A/B 再合并。
    """
    backend_dir = Path(__file__).resolve().parents[1]
    code = "from app.retrieval.config import HYDE_ENABLED; print(HYDE_ENABLED)"
    # 清掉 RETRIEVAL_HYDE 再问「默认值是什么」；其余 env 保留（含 .env 被 load_dotenv
    # 读到的部署实况，那正是本测试想钉住的东西）。
    env = {k: v for k, v in os.environ.items() if k != "RETRIEVAL_HYDE"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"子进程探测失败：{proc.stderr[:300]}"
    assert proc.stdout.strip() == "False", (
        "未设置 RETRIEVAL_HYDE 时 HYDE_ENABLED 仍为 True —— "
        "「本改动对生产零影响」的结论已失效，需补 HyDE 开启态的检索指标 A/B。"
        "注意子进程会读 `backend/.env`（config 顶层有 load_dotenv），"
        "所以这条同时钉住「代码默认值」与「本机部署实况」两件事。"
    )


def test_hyde_unavailable_client_is_not_called(monkeypatch):
    """LLM 不可用时不得发起调用（原则 7：HyDE 失败不阻断检索）。

    ⚠️ 这里**必须用「记录调用」而不是「抛 AssertionError」**来探测
    （subagent 审查抓出）：`_hyde_text` 末尾是 `except Exception`，
    假客户端抛的 `AssertionError` 会被它吞掉、函数照常返回 None，
    测试**照样绿**——删掉 `if not client.available` 守卫它也发现不了。
    """
    class _Unavailable:
        available = False

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *a, **k):
            self.calls += 1
            return {"choices": [{"message": {"content": "不该出现的文本"}}]}

    fake = _Unavailable()
    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **k: fake)

    assert pipeline._hyde_text({"query": "油耗多少"}) is None
    assert fake.calls == 0, "LLM 不可用时仍然发起了调用"


def test_disabled_thinking_wire_format():
    """线格式：`disabled` 必须译成 `{"type": "disabled"}` 且**不带** reasoning_effort。"""
    p = llm_mod._chat_payload("deepseek-flash", [{"role": "user", "content": "x"}], None, 0.1, False, "disabled")
    assert p["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in p
    # 对照：不传时端点默认是 enabled + high，也就是原来 HyDE 的实际行为
    default_p = llm_mod._chat_payload("deepseek-flash", [{"role": "user", "content": "x"}], None, 0.1, False, None)
    assert "thinking" not in default_p and "reasoning_effort" not in default_p
