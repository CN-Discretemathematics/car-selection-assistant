"""H3：`async def` 里不得直接调同步的 DB 操作（阻塞事件循环）。

## 缺陷

`backend/app/agent/engine.py` 修复前有 9 处：推荐 / 版本差异 / 车系问答三条回答链
各查一次 `Source`、5 处 `_source_citations`、1 处 `_unlock_note`。
这些点每次都是**同步 SQL**——真正执行 I/O 的是**结果被消费**那一刻
（`.all()`），不是 `db.scalars()` 本身，所以**只包住调用不包住消费等于没修**。

## 两条守卫

1. **AST 契约**（结构不变式）：async 函数体里出现
   - `db.<方法>(...)` 调用，或
   - 直接调用本模块里「同步且会碰 db」的函数
   → 阻塞事件循环。

   `run_in_threadpool(lambda: ...)` 的 lambda 体**不算**——它本来就在别的线程跑。

2. **行为证据**：慢 DB 调用期间，事件循环必须仍在跑（否则并发请求被一起拖住）。
   这条是「包没包对」的判据：包住 `db.scalars()` 但没包住 `.all()`，循环照样被卡。
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.agent import engine as engine_mod
from app.agent.engine import get_agent_engine
from app.agent.session import get_session_store
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

ENGINE_PATH = Path(engine_mod.__file__)


def _callee(call: ast.Call) -> str | None:
    """被调用的名字。`self._x` 归一为 `_x`（方法与函数同表）。"""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id + "." + func.attr
    return None


def _bare(name: str | None) -> str | None:
    """去掉 `self.` 前缀——方法与函数要在同一张表里比对。"""
    if name is None:
        return None
    return name.split(".")[-1]


def _touches_db(node: ast.AST) -> bool:
    """该函数体内是否直接出现了 `db.*` 调用。"""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "db"
        for n in ast.walk(node)
    )


def _sync_db_helpers(tree: ast.AST) -> set[str]:
    """本模块里「同步且会（直接或间接）碰 db」的函数名。

    **必须算传递闭包**：`_source_citations` 自己不碰 db（它调 `_source_names`），
    但它照样是同步 DB 操作。只算「直接碰 db」的话，抽出一个 helper 就能让守卫失明
    （2026-10-21 变异测试 M2 实测：`_source_citations` 退回裸调，用例却全绿）。
    """
    funcs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    helpers = {name for name, node in funcs.items() if _touches_db(node)}
    changed = True
    while changed:
        changed = False
        for name, node in funcs.items():
            if name in helpers:
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    callee = _bare(_callee(call))
                    if callee and callee in helpers:
                        helpers.add(name)
                        changed = True
                        break
    return helpers


def _blocking_calls(node: ast.AST, helpers: set[str]):
    """产出 async 函数体里的阻塞点（行号, 名称）。

    `run_in_threadpool(...)` 的 lambda 体跳过——它在别的线程执行。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Call):
            name = _callee(child)
            if name == "run_in_threadpool":
                for arg in child.args:
                    if not isinstance(arg, ast.Lambda):
                        yield from _blocking_calls(arg, helpers)
                for kw in child.keywords:
                    yield from _blocking_calls(kw.value, helpers)
                continue
            if name and name.startswith("db."):
                yield (child.lineno, name)
                continue
            bare = _bare(name)
            if bare and bare in helpers:
                yield (child.lineno, name)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 嵌套定义的函数有自己的作用域，不算外层 async 的阻塞点
            continue
        yield from _blocking_calls(child, helpers)


def test_no_sync_db_call_inside_async_functions():
    """async 函数里不得直接碰同步 DB（结构判据，精确到行）。"""
    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    helpers = _sync_db_helpers(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for lineno, name in _blocking_calls(node, helpers):
            violations.append(f"{node.name}:{lineno} → {name}")
    assert not violations, (
        "以下 async 函数里仍有同步 DB 调用（会阻塞事件循环）：\n  "
        + "\n  ".join(violations)
    )


def test_source_query_has_exactly_one_implementation():
    """查 Source 的实现只应有一处——此前 4 处各写一遍（同一件事写四遍必然漂移）。"""
    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    hits = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "select"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "Source"
    ]
    assert len(hits) == 1, f"查 Source 的实现应只有一处，实际 {len(hits)} 处：行 {hits}"


# ── 行为证据：慢 DB 期间事件循环仍在跑 ─────────────────────────────────────


@pytest.fixture()
def seeded_db(db_session: Session) -> Session:
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    series = make_series(
        db_session, brand, name="通勤家轿", body_type="sedan", energy_types=("BEV",), source=source
    )
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV",
        price_cny="99800",
        facts=[("座位数", "seats", "5", "座", None), ("尺寸", "length_mm", "4680", "mm", None)],
        source=source,
    )
    db_session.commit()
    return db_session


def test_slow_db_does_not_block_the_event_loop(seeded_db: Session, monkeypatch):
    """慢 DB 调用期间事件循环必须仍在跑。

    这是「包没包对」的判据：只把 `db.scalars()` 挪进线程池、却把 `.all()` 留在
    外面（或干脆不挪），循环照样被卡住——本用例会红。

    ⚠️ 必须先断言「这一轮确实走了查 Source 的路径」：否则测的是空气——
    语料若停在追问分支，推荐链根本没跑，慢 DB 一次都没被调用，用例恒绿
    （2026-10-21 实测踩到：只发「预算12万」走的是追问路径）。
    """
    original = engine_mod._source_names
    calls = {"n": 0}
    # 只统计「慢 DB 正在执行」这段时间里的 tick——否则阻塞窗口**之外**的 tick
    # 也会被算进去，测出来的数字虚高（2026-10-21 实测踩到：两种实现都「通过」）。
    blocking = {"on": False}

    def slow(db, source_ids):
        calls["n"] += 1
        blocking["on"] = True
        try:
            time.sleep(0.3)
        finally:
            blocking["on"] = False
        return original(db, source_ids)

    monkeypatch.setattr(engine_mod, "_source_names", slow)

    engine = get_agent_engine()
    sid = get_session_store().create()

    async def scenario() -> int:
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                await asyncio.sleep(0.005)
                if blocking["on"]:
                    ticks += 1

        task = asyncio.create_task(ticker())
        # 预算 + 用途 + 人数：三条硬约束齐了才会走推荐链（否则停在追问分支）
        await engine.respond(seeded_db, sid, "预算12万，通勤，两三个人")
        stop = True
        task.cancel()
        return ticks

    ticks = asyncio.run(scenario())
    assert calls["n"] >= 1, (
        "前提不成立：这一轮没走查 Source 的路径（慢 DB 一次都没被调用，测的是空气）"
    )
    assert ticks >= 5, (
        f"慢 DB 调用期间事件循环被阻塞（只 tick 了 {ticks} 次）——"
        f"说明 DB 操作仍在事件循环线程上执行"
    )
