# -*- coding: utf-8 -*-
"""影子档必须**看得见**品牌排除项（2026-10-22 生产实测发现）。

## 背景（生产实测，不是推演）

生产 `shadow` 档跑 13 条代表性探针时，**3 条品牌排除探针**
（「不考虑比亚迪」「不要特斯拉」「别要大众，也别要长安」）的影子日志
**一条 `brand_exclude` 都没有**——而同期 `budget` / `body_type` /
`usage` / `passengers` / `avoid` / `energy_preference` 六键全部正常出现。

根因：`effective_brand_exclusions` 整个函数被 `refill` 门短路，
而 `shadow` 档 `refill=False` ⇒ 它恒返回 `{}`
⇒ `persist_hints` 里那段专门为 `brand_exclude` 写的赋值
（`effective["brand_exclude"] = labels`）在 `shadow` 下是**死代码**。

⇒ 后果不只是「少一个键」：**观察期看不见 7 个被跟踪键里的 1 个**，
「观察够了就切 `on`」这道判据本身是**残的**——切过去才发现品牌排除没被记住。

## 本文件的判据

1. `shadow` 档：影子日志里**看得见** `brand_exclude`（值是品牌**名**，不是 id）。
2. `shadow` 档：**仍然不写库**（`preference_memories` 保持空）。
3. `shadow` 档：**仍然不回填画像**（推荐结果逐位不变）——「零行为变更」的保证。
4. `on` 档：照旧写入（回归护栏）。
5. `off` 档：照旧什么都不算（连身份都不解析）。
"""
from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy.orm import Session

from app.agent.preferences import (
    MODE_ENV,
    effective_brand_exclusions,
    get_or_create_identity,
    list_memories,
    persist_hints,
    restore_into_profile,
)
from app.agent.schemas import UserProfile
from app.catalog.brands import resolve_brand_mentions
from tests.seed import make_brand, make_source

_SHADOW_PREFIX = "偏好记忆（shadow，未写库）: "


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _seed_brand(db_session: Session, name: str = "测试比亚迪") -> None:
    source = make_source(db_session, name="官方测试来源")
    make_brand(db_session, name=name, source=source)
    db_session.commit()


def _identity(db_session: Session):
    identity, token, created = get_or_create_identity(db_session)
    db_session.commit()
    assert created is True
    assert token
    return identity, token


def _identity_headers(client) -> dict[str, str]:
    ident = client.post("/api/v1/agent/identity").json()
    return {"Authorization": f"Bearer {ident['token']}"}


def _create_session(client) -> str:
    resp = client.post("/api/v1/agent/sessions")
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _shadow_payload(caplog) -> dict:
    """从影子日志里取出最后一条「偏好记忆（shadow，未写库）」的 JSON 载荷。"""
    payloads = [
        json.loads(record.getMessage().split(_SHADOW_PREFIX, 1)[1])
        for record in caplog.records
        if record.name == "app.agent.preferences" and _SHADOW_PREFIX in record.getMessage()
    ]
    assert payloads, f"影子日志一条都没有：{[r.getMessage() for r in caplog.records]}"
    return payloads[-1]


def _turn_brand_hints(db_session: Session, message: str) -> dict:
    """本轮的品牌线索（解析层）——「不要 X」在这里就该变成排除项。"""
    return resolve_brand_mentions(db_session, message)


# ── 1. shadow 档：算得出、也看得见 ─────────────────────────────────────────


def test_shadow_mode_computes_brand_exclusions(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """`shadow` 档必须**算得出**本轮的品牌排除项（此前恒为 {}）。"""
    monkeypatch.setenv(MODE_ENV, "shadow")
    _seed_brand(db_session)
    identity, _ = _identity(db_session)

    turn = _turn_brand_hints(db_session, "预算20万，通勤，不要测试比亚迪")
    # 前提：解析层认得这个排除项（否则本用例证明不了任何事）
    assert turn.get("brand_exclude_labels") == ["测试比亚迪"], turn

    out = effective_brand_exclusions(db_session, identity, UserProfile(), turn)

    assert out.get("brand_exclude_labels") == ["测试比亚迪"], out
    assert out.get("brand_exclude_ids"), out


def test_shadow_mode_logs_brand_exclude(db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog):
    """影子日志里**看得见** `brand_exclude`——值是品牌名，不是 id。"""
    monkeypatch.setenv(MODE_ENV, "shadow")
    _seed_brand(db_session)
    identity, _ = _identity(db_session)
    turn = _turn_brand_hints(db_session, "预算20万，通勤，不要测试比亚迪")

    with caplog.at_level(logging.INFO, logger="app.agent.preferences"):
        persist_hints(
            db_session,
            identity,
            {"usage": ["通勤"]},
            brand_exclusions=effective_brand_exclusions(db_session, identity, UserProfile(), turn),
        )

    payload = _shadow_payload(caplog)
    assert payload.get("brand_exclude") == ["测试比亚迪"], payload
    assert payload.get("usage") == ["通勤"], payload


def test_shadow_log_branch_for_brand_exclude_is_not_dead(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    """`persist_hints` 里为 `brand_exclude` 准备的那段赋值**不得是死代码**。

    这条专门钉「影子日志的 brand_exclude 分支在 shadow 下不可达」这一类缺陷：
    生产实测里它安静地不可达了一个影子周期，观察期据此得出的结论是残的。
    """
    monkeypatch.setenv(MODE_ENV, "shadow")
    identity, _ = _identity(db_session)

    with caplog.at_level(logging.INFO, logger="app.agent.preferences"):
        persist_hints(
            db_session,
            identity,
            {},
            brand_exclusions={"brand_exclude_ids": [1], "brand_exclude_labels": ["测试比亚迪"]},
        )

    assert _shadow_payload(caplog).get("brand_exclude") == ["测试比亚迪"]


# ── 2. shadow 档：仍然不写库、不回填（零行为变更）─────────────────────────


def test_shadow_mode_writes_nothing(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """看得见 ≠ 写进库：`shadow` 档 `preference_memories` 仍须为空。"""
    monkeypatch.setenv(MODE_ENV, "shadow")
    _seed_brand(db_session)
    identity, _ = _identity(db_session)
    turn = _turn_brand_hints(db_session, "预算20万，通勤，不要测试比亚迪")

    persist_hints(
        db_session,
        identity,
        {"usage": ["通勤"]},
        brand_exclusions=effective_brand_exclusions(db_session, identity, UserProfile(), turn),
    )

    assert list_memories(db_session, identity) == []


def test_shadow_mode_does_not_refill_profile(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """**零行为变更**的保证：算得出 ≠ 回填——画像不得被品牌排除项改写。

    `refill`（回填进画像）会改变推荐结果，所以 `shadow` 绝不能做；
    这道门在 `restore_into_profile` 自己身上，与本文件的改动无关。
    """
    monkeypatch.setenv(MODE_ENV, "shadow")
    _seed_brand(db_session)
    identity, _ = _identity(db_session)
    turn = _turn_brand_hints(db_session, "预算20万，通勤，不要测试比亚迪")

    exclusions = effective_brand_exclusions(db_session, identity, UserProfile(), turn)
    assert exclusions, "前提不成立：shadow 档算不出排除项，本用例测的是空气"

    profile = restore_into_profile(
        db_session, identity, UserProfile(), brand_exclusions=exclusions
    )

    assert profile.brand_exclude_ids == [], profile
    assert profile.brand_exclude_labels == [], profile


# ── 3. on / off 两档：照旧 ─────────────────────────────────────────────────


def test_on_mode_still_writes_brand_exclude(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """回归护栏：`on` 档照旧把品牌排除项**写进库**（值是品牌名）。"""
    monkeypatch.setenv(MODE_ENV, "on")
    _seed_brand(db_session)
    identity, _ = _identity(db_session)
    turn = _turn_brand_hints(db_session, "预算20万，通勤，不要测试比亚迪")

    persist_hints(
        db_session,
        identity,
        {"usage": ["通勤"]},
        brand_exclusions=effective_brand_exclusions(db_session, identity, UserProfile(), turn),
    )

    rows = list_memories(db_session, identity)
    brand_rows = [r for r in rows if r.key == "brand_exclude"]
    assert brand_rows, f"品牌排除项应当被写入，实际写入：{[r.key for r in rows]}"
    assert brand_rows[0].value == '["测试比亚迪"]', brand_rows[0].value


def test_off_mode_computes_nothing(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """`off` 档照旧什么都不算——它连身份都不解析。"""
    monkeypatch.setenv(MODE_ENV, "off")
    _seed_brand(db_session)
    identity, _ = _identity(db_session)
    turn = _turn_brand_hints(db_session, "预算20万，通勤，不要测试比亚迪")

    assert effective_brand_exclusions(db_session, identity, UserProfile(), turn) == {}


# ── 4. 端到端：HTTP 链路上的 shadow 观察 ───────────────────────────────────


def test_shadow_mode_end_to_end_logs_brand_exclude(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    """走完整 HTTP 链路（代理 → 引擎 → 记忆落点）时，影子日志也看得见排除项。

    这是生产实测那条现象的**同构复现**：探针从 HTTP 入口进来，
    断言落在影子日志的载荷上。
    """
    monkeypatch.setenv(MODE_ENV, "shadow")
    _seed_brand(db_session)
    headers = _identity_headers(client)
    sid = _create_session(client)

    with caplog.at_level(logging.INFO, logger="app.agent.preferences"):
        resp = client.post(
            f"/api/v1/agent/sessions/{sid}/messages",
            json={"message": "预算20万，通勤，不要测试比亚迪"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text

    payload = _shadow_payload(caplog)
    assert payload.get("brand_exclude") == ["测试比亚迪"], payload
    # 前提之外再加一条：观察到了，但一个字都没写进库
    assert client.get("/api/v1/agent/preferences", headers=headers).json() == []
