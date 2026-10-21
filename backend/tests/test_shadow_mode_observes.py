"""`shadow` 档必须**真的在观察**（2026-10-21 生产实测发现的空档）。

## 缺陷

路由层用 `is_enabled()`（**只在 `on` 为真**）决定「是否解析身份」，
而两条影子日志

- `preferences.persist_hints` → 「偏好记忆（shadow，未写库）」
- `feedback.record_rejection` → 「拒绝反馈（shadow，未写库）」

**都以「有身份」为前提**（开头都是 `if identity is None: return`）
⇒ `shadow` 档的身份恒为 None ⇒ 影子日志**永远不会触发**
⇒ `shadow` 成了**空档**：不写库（对）也不观察（错）。
「先观察一两天再切 on」那道观察期于是是**假的**——观察不到任何东西。

## 本组用例钉住三件事

1. `shadow` 档：影子日志**确实出现**，且日志里**真的有抽出来的预算**
   （否则「日志出现但内容是空的」也算恒真）；
2. `shadow` 档：**不写库**、**不回填**（回答与 `off` 档逐位相同）——
   并用 `on` 档做**阳性对照**：若 `on` 档的回答与 `off` 一样，
   那前两条断言都证明不了任何事（空转）；
3. `off` 档：身份**不被解析**（连查都不查——那是它省下的那次 SELECT）。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.preferences import (
    CATEGORY_CONSTRAINT,
    CATEGORY_USAGE,
    MODE_ENV,
    encode_value,
    get_or_create_identity,
    remember,
)
from app.common.models import PreferenceMemory
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_car(db_session: Session) -> None:
    """库里要有车，否则推荐链走不到「按预算筛」那一步，filters 恒为空
    ⇒ on/off 的对比会**恒等**（断言空转）。"""
    source = make_source(db_session, name="影子档测试来源")
    brand = make_brand(db_session, name="影子档测试品牌", source=source)
    series = make_series(
        db_session, brand, name="影子档测试车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV",
        price_cny="150000", source=source,
    )
    db_session.commit()


def _ask(client: TestClient, message: str, token: str | None = None) -> dict:
    """问一轮，返回结构化结果。"""
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": message},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _missing(answer: dict) -> list[str]:
    """本轮追问**缺哪几项**（结构化字段，不查正文）。"""
    clarification = answer.get("clarification") or {}
    return list(clarification.get("missing") or [])


def _identity_with_memories(db_session: Session, max_cny: float = 150000.0) -> tuple[object, str]:
    """造一个**已记住预算 + 用途**的身份（跨会话回填的载体）。

    为什么记两样：Agent 在**缺约束**时会追问（「预算？用途？」）⇒ 只记预算的话，
    on 档仍会追问 ⇒ 推荐结果与 off 档一样 ⇒ 对比**恒等**（空转）。
    记上用途后，on 档能直接给推荐 ⇒ 两个档位的差异才**可判**。
    """
    identity, token, _created = get_or_create_identity(db_session)
    db_session.commit()
    remember(
        db_session,
        identity,
        category=CATEGORY_CONSTRAINT,
        key="budget",
        value=encode_value("budget", {"max": max_cny}),
        source_type="explicit",
        confirmed=True,
    )
    remember(
        db_session,
        identity,
        category=CATEGORY_USAGE,
        key="usage",
        value=encode_value("usage", ["通勤"]),
        source_type="explicit",
        confirmed=True,
    )
    db_session.commit()
    return identity, token


def test_shadow_mode_emits_a_shadow_log_with_real_content(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    """`shadow` 档：影子日志**真的出现**，且里面有抽出来的预算（不是空壳）。"""
    monkeypatch.setenv(MODE_ENV, "shadow")
    ident = client.post("/api/v1/agent/identity").json()
    token = ident["token"]
    assert token, "身份端点应当返回一次明文 token"

    with caplog.at_level(logging.INFO, logger="app.agent.preferences"):
        _ask(client, "预算15万", token)

    messages = [r.getMessage() for r in caplog.records]
    shadow_lines = [m for m in messages if "偏好记忆" in m and "shadow" in m]
    assert shadow_lines, f"影子日志没出现（观察期会是空的）：{messages}"
    # 日志里必须有**真实抽出来的内容**，否则「日志出现」也证明不了什么
    assert any("budget" in m for m in shadow_lines), f"影子日志里没有预算：{shadow_lines}"

    # 影子档**不得写库**
    assert db_session.query(PreferenceMemory).count() == 0, "shadow 档不得把记忆写进库"


def test_shadow_mode_does_not_restore_but_on_mode_does(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """`shadow` 不回填、`on` 回填——后者是前者的**阳性对照**。

    若 `on` 档的回答与 `off` 档相同（记忆没起作用），那「shadow 与 off 相同」
    这条断言就**证明不了任何事**（空转）。所以两条都要断言。
    """
    # ⚠️ 必须**先在 on 档**造这些记忆：`remember()` 自己按闸门拒绝写入
    # （off/shadow 都「不写，只落日志」）⇒ 在 off 档造 ⇒ 行是空的 ⇒ 后面全是空转。
    monkeypatch.setenv(MODE_ENV, "on")
    _seed_car(db_session)
    _identity, token = _identity_with_memories(db_session)
    ask = "按我之前说的条件推荐"
    assert db_session.query(PreferenceMemory).count() == 2, "前提：记忆确实写进去了"

    monkeypatch.setenv(MODE_ENV, "off")
    off = _ask(client, ask)
    monkeypatch.setenv(MODE_ENV, "shadow")
    shadow = _ask(client, ask, token)
    monkeypatch.setenv(MODE_ENV, "on")
    on = _ask(client, ask, token)

    # 判据用**结构化字段**（追问缺什么），不查正文：
    # 缺约束时 Agent 会追问 ⇒ 记忆回填后，缺的那一项会**变少**。
    #   off    ：缺 budget（什么都没记住）
    #   shadow：缺 budget（不许回填）
    #   on    ：缺 passengers（预算与用途已被记忆填上）
    assert _missing(shadow) == _missing(off), (
        f"shadow 档**不得回填**（回填会改变推荐结果，那就不是观察了）："
        f"shadow 缺 {_missing(shadow)}，off 缺 {_missing(off)}"
    )
    assert _missing(on) != _missing(off), (
        "on 档**应当回填**已记住的预算与用途（缺的那一项应当变少）"
        "——若这条不成立，上面那条「shadow 与 off 相同」就是空转断言"
    )
    assert "budget" in _missing(off), f"前提不成立：off 档本该缺预算，实际缺 {_missing(off)}"
    assert "budget" not in _missing(on), (
        f"on 档应当记得预算（它只缺人数），实际缺 {_missing(on)}"
    )


def test_off_mode_does_not_resolve_the_identity_but_shadow_does(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """`off` 连查都不查（省下那次 SELECT）；`shadow` 必须查（否则观察不到）。"""
    from app.agent import router as router_mod

    calls: list[str] = []
    original = router_mod._resolve_identity

    def _spy(db, authorization):  # noqa: ANN001 - 测试替身
        calls.append(authorization or "")
        return original(db, authorization)

    monkeypatch.setattr(router_mod, "_resolve_identity", _spy)
    # ⚠️ 先领身份再装探针：领身份这个端点**自己**也调 `_resolve_identity`
    # （带上已有 token 时返回同一个身份）——不先领就会把那次调用算进来。
    ident = client.post("/api/v1/agent/identity").json()
    token = ident["token"]
    calls.clear()

    monkeypatch.setenv(MODE_ENV, "off")
    _ask(client, "预算15万", token)
    assert calls == [], f"off 档不应解析身份（省一次 SELECT），实际解析了 {len(calls)} 次"

    calls.clear()
    monkeypatch.setenv(MODE_ENV, "shadow")
    _ask(client, "预算15万", token)
    assert calls, "shadow 档**必须**解析身份——不解析就观察不到任何东西"
