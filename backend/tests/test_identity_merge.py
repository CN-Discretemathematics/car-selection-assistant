"""账户合并与隐私（计划 Phase 3/4「账户合并与隐私」）。

## 三件事

1. **合并**：登录后把**匿名身份**积累的记忆与拒绝反馈搬进**账户的身份**，
   并让**匿名 token 失效**（迁移的 docstring 已定这个语义 ✓）。
2. **导出**：用户能看自己被记住了什么（记忆是黑箱的话，个性化就谈不上可信 ✓）。
3. **删除**：用户能把这些数据全删掉（撤回权 ✓）。

## 去重是这里最容易出错的地方

记忆表「一个键只留一行」（`(identity, category, key)` 唯一 ✓）⇒ 合并时同一个键
只能留一行 ⇒ 取**较新**的（记忆是「当前状态」不是流水账 ✓）。
反馈表按 `(identity, variant_id)` 唯一 ✓ ⇒ 同理 ✓。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.identity_merge import (
    account_identity,
    delete_identity_data,
    export_identity_data,
    merge_anonymous_into_user,
)
from app.agent.preferences import (
    MODE_ENV,
    encode_value,
    get_identity_by_token,
    get_or_create_identity,
    remember,
)
from app.auth.security import get_token_store
from app.common.models import AnonymousIdentity, PreferenceMemory, User, VariantFeedback
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


@pytest.fixture()
def durable_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV, "on")


def _seed_car(db_session: Session) -> int:
    source = make_source(db_session, name="合并测试来源")
    brand = make_brand(db_session, name="合并测试品牌", source=source)
    series = make_series(
        db_session, brand, name="合并车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    year = make_year(db_session, series)
    variant = make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV",
        price_cny="150000", source=source,
    )
    db_session.commit()
    return variant.id


def _user(db_session: Session) -> User:
    user = User(email="merge@example.com", status="active")
    db_session.add(user)
    db_session.commit()
    return user


def _anonymous_with_memory(db_session: Session) -> tuple[AnonymousIdentity, str]:
    identity, token, _created = get_or_create_identity(db_session)
    db_session.commit()
    remember(
        db_session,
        identity,
        category="usage",
        key="usage",
        value=encode_value("usage", ["通勤"]),
        source_type="explicit",
        confirmed=True,
    )
    db_session.commit()
    assert token
    return identity, token


# ── 合并 ────────────────────────────────────────────────────────────────────


def test_merge_moves_memories_into_the_account_identity(
    db_session: Session, durable_on: None
):
    """合并后，记忆在**账户的身份**下，匿名身份下没有 ✓。"""
    _seed_car(db_session)
    user = _user(db_session)
    anonymous, _token = _anonymous_with_memory(db_session)

    outcome = merge_anonymous_into_user(db_session, anonymous, user)

    assert outcome.memories_moved == 1
    assert outcome.token, "账户身份新建时应把 token 给一次（明文只给这一次）"
    account = account_identity(db_session, user)
    assert account is not None
    assert db_session.query(PreferenceMemory).filter_by(identity_id=account.id).count() == 1
    assert (
        db_session.query(PreferenceMemory).filter_by(identity_id=anonymous.id).count() == 0
    )


def test_merged_anonymous_token_stops_working(
    db_session: Session, durable_on: None
):
    """合并后**匿名 token 失效** ⇒ 查不到身份 ✓（数据不能分叉到两个身份上 ✓）。"""
    _seed_car(db_session)
    user = _user(db_session)
    anonymous, token = _anonymous_with_memory(db_session)

    merge_anonymous_into_user(db_session, anonymous, user)

    assert get_identity_by_token(db_session, token) is None
    assert anonymous.status == "merged"
    assert anonymous.merged_user_id == user.id


def test_merge_keeps_the_newer_memory_on_the_same_key(
    db_session: Session, durable_on: None
):
    """同一个键合并后**只留一行**，取**较新**的那条 ✓。

    记忆是「当前状态」不是流水账 ⇒ 留两条会让回填时二义 ✓。
    """
    _seed_car(db_session)
    user = _user(db_session)
    anonymous, _token = _anonymous_with_memory(db_session)
    account = account_identity(db_session, user)
    if account is None:
        account, _t, _c = get_or_create_identity(db_session)
        account.merged_user_id = user.id
        db_session.commit()
    # 账户侧先有一条同键但**更旧**的记忆
    remember(
        db_session,
        account,
        category="usage",
        key="usage",
        value=encode_value("usage", ["长途"]),
        source_type="explicit",
        confirmed=True,
    )
    db_session.commit()
    older = db_session.query(PreferenceMemory).filter_by(identity_id=account.id).one()
    # 把账户侧那条的��间**拨到过去** ⇒ 匿名侧那条更新 ⇒ 合并后应胜出
    past = older.created_at - timedelta(days=1)
    older.created_at = past
    older.updated_at = past
    db_session.commit()

    merge_anonymous_into_user(db_session, anonymous, user)

    rows = db_session.query(PreferenceMemory).filter_by(identity_id=account.id).all()
    assert len(rows) == 1, rows
    assert rows[0].value == encode_value("usage", ["通勤"]), "匿名侧那条更新 ⇒ 应胜出"


def test_merge_is_idempotent(db_session: Session, durable_on: None):
    """重复合并**不会**把数据搬两次 ✓。"""
    _seed_car(db_session)
    user = _user(db_session)
    anonymous, _token = _anonymous_with_memory(db_session)

    first = merge_anonymous_into_user(db_session, anonymous, user)
    second = merge_anonymous_into_user(db_session, anonymous, user)

    assert first.memories_moved == 1
    assert second.memories_moved == 0
    account = account_identity(db_session, user)
    assert db_session.query(PreferenceMemory).filter_by(identity_id=account.id).count() == 1


# ── 隐私：导出与删除 ────────────────────────────────────────────────────────


def test_export_lists_the_stored_data(db_session: Session, durable_on: None):
    """导出把记忆与反馈原样列出来 ✓（用户有权看自己被记住了什么 ✓）。"""
    _seed_car(db_session)
    user = _user(db_session)
    anonymous, _token = _anonymous_with_memory(db_session)
    merge_anonymous_into_user(db_session, anonymous, user)
    account = account_identity(db_session, user)

    data = export_identity_data(db_session, account)

    assert data["identity_id"] == account.id
    assert len(data["memories"]) == 1
    assert data["memories"][0]["key"] == "usage"
    assert data["variant_feedbacks"] == []


def test_delete_removes_all_personalization_data(
    db_session: Session, durable_on: None
):
    """删除后记忆与反馈**全没了** ✓。"""
    _seed_car(db_session)
    user = _user(db_session)
    anonymous, _token = _anonymous_with_memory(db_session)
    merge_anonymous_into_user(db_session, anonymous, user)
    account = account_identity(db_session, user)
    db_session.add(
        VariantFeedback(
            identity_id=account.id, variant_id=1, series_id=1, reason="不要"
        )
    )
    db_session.commit()

    counts = delete_identity_data(db_session, account)

    assert counts["memories_deleted"] == 1
    assert counts["feedbacks_deleted"] == 1
    assert db_session.query(PreferenceMemory).filter_by(identity_id=account.id).count() == 0
    assert db_session.query(VariantFeedback).filter_by(identity_id=account.id).count() == 0


# ── API ─────────────────────────────────────────────────────────────────────


def test_merge_endpoint_moves_the_data(
    client: TestClient, db_session: Session, durable_on: None
):
    """接口层：带账户令牌调合并端点 ⇒ 数据搬过去 ✓。"""
    _seed_car(db_session)
    user = _user(db_session)
    _anonymous, token = _anonymous_with_memory(db_session)
    account_token = get_token_store().issue(user.id)

    resp = client.post(
        "/api/v1/auth/merge-identity",
        json={"anonymous_token": token},
        headers={"Authorization": f"Bearer {account_token}"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["memories_moved"] == 1
    assert body["token"], "账户身份新建时应返回一次明文 token"
    account = account_identity(db_session, user)
    assert db_session.query(PreferenceMemory).filter_by(identity_id=account.id).count() == 1


def test_export_and_delete_endpoints(
    client: TestClient, db_session: Session, durable_on: None
):
    """导出 / 删除两个端点 ✓。"""
    _seed_car(db_session)
    user = _user(db_session)
    anonymous, _token = _anonymous_with_memory(db_session)
    merge_anonymous_into_user(db_session, anonymous, user)
    account_token = get_token_store().issue(user.id)
    headers = {"Authorization": f"Bearer {account_token}"}

    exported = client.get("/api/v1/me/data", headers=headers)
    assert exported.status_code == 200, exported.text
    assert len(exported.json()["memories"]) == 1

    deleted = client.delete("/api/v1/me/data", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["memories_deleted"] == 1
