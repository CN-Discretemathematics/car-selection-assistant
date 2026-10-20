"""跨会话偏好记忆（Phase 1）：身份、封闭词表、回填、覆盖与矛盾消解。

## 这组用例守的是什么

1. **三模式闸门**：`off` 时**完全不介入**（读 + 写都短路），不只「不写库」；
2. **封闭词表 + 推断禁区**：写不进影响不了推荐的东西，也不让模型推断碰硬约束；
3. **回填的时序**：本轮说的压过上次记的；会话内说过的压过旧记忆；
4. **矛盾消解**：同一个值不能既被要求又被排除（否则候选集恒空）；
5. **跨会话真的有效**：第二段对话不再重复问预算（真后端 + 真结构断言）。
"""
from __future__ import annotations

import importlib.util

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent import router as agent_router
from app.agent.engine import extract_hints
from app.agent.preferences import (
    CATEGORY_CONSTRAINT,
    CATEGORY_EXCLUSION,
    CATEGORY_USAGE,
    MODE_ENV,
    TRACKED_BY_KEY,
    effective_tracked,
    encode_value,
    forget,
    get_identity_by_token,
    get_mode,
    get_or_create_identity,
    list_memories,
    memories_to_hints,
    persist_hints,
    remember,
    restore_into_profile,
    validate_memory,
)
from app.agent.schemas import Budget, UserProfile
from app.common.models import AnonymousIdentity, PreferenceMemory
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


@pytest.fixture()
def durable_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开持久化闸门（默认 off，测试必须显式打开）。"""
    monkeypatch.setenv(MODE_ENV, "on")


def _identity(db_session: Session) -> tuple[AnonymousIdentity, str]:
    """建一个身份，返回 (身份, 明文 token)。"""
    identity, token, created = get_or_create_identity(db_session)
    db_session.commit()
    assert created is True
    assert token
    return identity, token


def _seed_agent_data(db_session: Session) -> None:
    """Agent 端到端用的最小种子（车型足够触发推荐与追问）。"""
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    suv = make_series(
        db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source
    )
    sedan = make_series(
        db_session, brand, name="通勤家轿", body_type="sedan", energy_types=("BEV",), source=source
    )
    year_suv, year_sedan = make_year(db_session, suv), make_year(db_session, sedan)
    make_variant(
        db_session, suv, year_suv, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[
            ("座位数", "seats", "5", "座", None),
            ("尺寸", "length_mm", "4650", "mm", None),
        ],
        source=source,
    )
    make_variant(
        db_session, sedan, year_sedan, config_version="标准版", energy_type="BEV", price_cny="99800",
        facts=[
            ("座位数", "seats", "5", "座", None),
            ("尺寸", "length_mm", "4680", "mm", None),
        ],
        source=source,
    )
    db_session.commit()


def _create_session(client: TestClient) -> str:
    resp = client.post("/api/v1/agent/sessions")
    assert resp.status_code == 201
    return resp.json()["session_id"]


# ── 模式闸门 ────────────────────────────────────────────────────────────────


def test_mode_defaults_to_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MODE_ENV, raising=False)
    assert get_mode() == "off"


def test_garbage_mode_falls_back_to_off(monkeypatch: pytest.MonkeyPatch):
    """非法取值必须回退到安全侧（不写库、不回填），不能当成 on。"""
    monkeypatch.setenv(MODE_ENV, "on_typo")
    assert get_mode() == "off"


def test_off_mode_writes_nothing(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MODE_ENV, "off")
    identity, _ = _identity(db_session)
    outcome = remember(
        db_session,
        identity,
        category=CATEGORY_USAGE,
        key="usage",
        value=encode_value("usage", ["通勤"]),
        confirmed=True,
    )
    assert outcome.written is False
    assert list_memories(db_session, identity) == []


def test_shadow_mode_writes_nothing(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """shadow = 只落日志：既不写库，也不回填（回填会改变推荐结果，那就不是观察了）。"""
    monkeypatch.setenv(MODE_ENV, "shadow")
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"usage": ["通勤"]})
    assert list_memories(db_session, identity) == []
    profile = UserProfile()
    restore_into_profile(db_session, identity, profile)
    assert profile.usage == []


def test_off_mode_does_not_restore(db_session: Session, durable_on, monkeypatch: pytest.MonkeyPatch):
    """**off 不只是不写库，是完全不介入**——否则切回 off 时旧记忆仍在悄悄影响推荐。"""
    identity, _ = _identity(db_session)
    remember(
        db_session,
        identity,
        category=CATEGORY_USAGE,
        key="usage",
        value=encode_value("usage", ["通勤"]),
        confirmed=True,
    )
    db_session.commit()
    monkeypatch.setenv(MODE_ENV, "off")
    profile = UserProfile()
    restore_into_profile(db_session, identity, profile)
    assert profile.usage == []


# ── 身份 ────────────────────────────────────────────────────────────────────


def test_identity_stores_only_the_token_hash(db_session: Session):
    """**库里只存哈希**：库被拖走也无法反推出可用凭据。"""
    identity, token = _identity(db_session)
    row = db_session.get(AnonymousIdentity, identity.id)
    assert row is not None
    assert row.token_hash != token
    assert len(row.token_hash) == 64
    all_hashes = [r.token_hash for r in db_session.query(AnonymousIdentity).all()]
    assert token not in all_hashes


def test_same_token_resolves_to_the_same_identity(db_session: Session):
    identity, token = _identity(db_session)
    again, plaintext, created = get_or_create_identity(db_session, token)
    assert again.id == identity.id
    assert created is False
    assert plaintext is None


def test_unknown_token_resolves_to_none(db_session: Session):
    assert get_identity_by_token(db_session, "not-a-real-token") is None
    assert get_identity_by_token(db_session, "") is None


# ── 封闭词表与推断禁区 ──────────────────────────────────────────────────────


def test_unknown_key_is_rejected():
    ok, reason = validate_memory(
        category=CATEGORY_CONSTRAINT,
        key="must_have",
        value='"天窗"',
        source_type="explicit",
    )
    assert ok is False
    assert "封闭词表" in reason


def test_value_outside_the_vocabulary_is_rejected():
    """用途值必须落在 `engine._USAGE_HINTS` 的值里——新造词表就是造噪声。"""
    ok, reason = validate_memory(
        category=CATEGORY_USAGE, key="usage", value='["飞行"]', source_type="explicit"
    )
    assert ok is False
    assert "封闭词表" in reason


def test_body_type_value_must_be_a_known_body():
    ok, _ = validate_memory(
        category=CATEGORY_CONSTRAINT,
        key="body_type",
        value='["truck"]',
        source_type="explicit",
    )
    assert ok is False


def test_inferred_cannot_write_hard_constraints():
    """推断猜错会**静默砍掉整个候选集**，用户看不到原因。"""
    for key, value in (
        ("body_type", encode_value("body_type", ["suv"])),
        ("energy_preference", encode_value("energy_preference", ["BEV"])),
        ("budget", encode_value("budget", {"max": 200000.0})),
    ):
        ok, reason = validate_memory(
            category=TRACKED_BY_KEY[key].category,
            key=key,
            value=value,
            source_type="inferred",
        )
        assert ok is False, key
        assert "推断禁区" in reason


def test_budget_bounds_are_checked():
    ok, _ = validate_memory(
        category=CATEGORY_CONSTRAINT,
        key="budget",
        value=encode_value("budget", {"min": 300000.0, "max": 200000.0}),
        source_type="explicit",
    )
    assert ok is False


def test_unconfirmed_memory_is_not_written(db_session: Session, durable_on):
    """只有用户明说或确认过的才落库。"""
    identity, _ = _identity(db_session)
    outcome = remember(
        db_session,
        identity,
        category=CATEGORY_USAGE,
        key="usage",
        value=encode_value("usage", ["通勤"]),
        confirmed=False,
    )
    assert outcome.written is False
    assert list_memories(db_session, identity) == []


# ── 写：覆盖语义 ────────────────────────────────────────────────────────────


def test_persist_overwrites_in_place(db_session: Session, durable_on):
    """一个键只留一行：新值覆盖旧值，行 id 稳定（用户删的是同一条）。"""
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"budget": {"max": 200000.0}})
    rows = list_memories(db_session, identity)
    assert len(rows) == 1
    first_id = rows[0].id

    persist_hints(db_session, identity, {"budget": {"max": 300000.0}})
    rows = list_memories(db_session, identity)
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert memories_to_hints(db_session, identity)["budget"]["max"] == 300000.0


def test_persist_keeps_usage_and_constraints(db_session: Session, durable_on):
    identity, _ = _identity(db_session)
    persist_hints(
        db_session,
        identity,
        {"usage": ["通勤"], "body_type": ["sedan"], "passengers": 2},
    )
    hints = memories_to_hints(db_session, identity)
    assert hints["usage"] == ["通勤"]
    assert hints["body_type"] == ["sedan"]
    assert hints["passengers"] == 2


def test_persist_does_not_persist_weights(db_session: Session, durable_on):
    """权重**刻意不持久化**：`merge_profile` 的权重是累加语义，跨会话恢复会叠加成
    「一句话叠三轮」。这是 Phase 2 的独立决策，不在 Phase 1 里含糊处理。"""
    identity, _ = _identity(db_session)
    hints = extract_hints("空间优先，动力也要强")
    assert hints.get("weights"), "前提：这一轮确实抽到了权重线索"
    effective = effective_tracked(db_session, identity, hints)
    assert "weights" not in effective
    persist_hints(db_session, identity, hints)
    assert list_memories(db_session, identity) == []


def test_persist_accumulates_exclusions_across_turns(db_session: Session, durable_on):
    """排除项是**并集**：「上个月说过的不要 X」不该被「这轮说的不要 Y」冲掉。"""
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"avoid": ["suv"]})
    persist_hints(db_session, identity, {"avoid": ["mpv"]})
    assert memories_to_hints(db_session, identity)["avoid"] == ["mpv", "suv"]


# ── 矛盾消解 ────────────────────────────────────────────────────────────────


def test_negative_turn_clears_the_older_requirement(db_session: Session, durable_on):
    """用户改主意了：「要 SUV」→「不要 SUV」→ 旧的正面诉求必须作废。"""
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"body_type": ["suv"]})
    assert memories_to_hints(db_session, identity)["body_type"] == ["suv"]

    persist_hints(db_session, identity, {"avoid": ["suv"]})
    hints = memories_to_hints(db_session, identity)
    assert "body_type" not in hints
    assert hints["avoid"] == ["suv"]


def test_positive_turn_clears_the_older_exclusion(db_session: Session, durable_on):
    """反过来也一样：「不要 SUV」→「SUV 也行」→ 旧的排除必须作废。"""
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"avoid": ["suv"]})

    persist_hints(db_session, identity, {"body_type": ["suv"]})
    hints = memories_to_hints(db_session, identity)
    assert hints["body_type"] == ["suv"]
    assert "avoid" not in hints


def test_conflict_inside_memory_resolves_to_exclusion(db_session: Session, durable_on):
    """记忆内部自相矛盾时以**排除**为准——宁可少推一台车，好过推一台用户排斥的车。

    这里手工造一条矛盾记忆（绕开写入路径的消解），模拟历史脏数据。
    """
    identity, _ = _identity(db_session)
    db_session.add(
        PreferenceMemory(
            identity_id=identity.id,
            category=CATEGORY_CONSTRAINT,
            key="body_type",
            value=encode_value("body_type", ["suv"]),
            source_type="explicit",
            confirmed=True,
        )
    )
    db_session.add(
        PreferenceMemory(
            identity_id=identity.id,
            category=CATEGORY_EXCLUSION,
            key="avoid",
            value=encode_value("avoid", ["suv"]),
            source_type="explicit",
            confirmed=True,
        )
    )
    db_session.commit()

    profile = UserProfile()
    restore_into_profile(db_session, identity, profile)
    assert profile.body_type == []
    assert profile.avoid == ["suv"]


def test_restore_drops_exclusion_when_this_turn_asks_for_it(
    db_session: Session, durable_on
):
    """本轮明说的正面诉求压过记忆里的旧排除（「SUV 也行」）。"""
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"avoid": ["suv"]})
    profile = UserProfile()
    restore_into_profile(
        db_session, identity, profile, turn_hints={"body_type": ["suv"]}
    )
    assert profile.avoid == []


# ── 回填的时序 ──────────────────────────────────────────────────────────────


def test_restore_fills_an_empty_profile(db_session: Session, durable_on):
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"body_type": ["suv"], "usage": ["通勤"]})
    profile = UserProfile()
    restore_into_profile(db_session, identity, profile)
    assert profile.body_type == ["suv"]
    assert profile.usage == ["通勤"]


def test_restore_does_not_override_the_session_value(db_session: Session, durable_on):
    """会话内的画像是**比记忆新**的事实：今天说的 30 万不能被上个月记的 20 万改回去。"""
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"budget": {"max": 200000.0}})
    profile = UserProfile(budget=Budget(min=250000.0, max=300000.0))
    restore_into_profile(db_session, identity, profile)
    assert profile.budget.max == 300000.0
    assert profile.budget.min == 250000.0


def test_restore_does_not_override_the_session_body_type(db_session: Session, durable_on):
    """同上，车身类型也算：会话里已经说了「轿车」，旧记忆里的「SUV」不该盖回来。"""
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"body_type": ["suv"]})
    profile = UserProfile(body_type=["sedan"])
    restore_into_profile(db_session, identity, profile)
    assert profile.body_type == ["sedan"]


def test_restore_is_idempotent_within_a_session(db_session: Session, durable_on):
    """同一段会话里回填不能把值越叠越高（否则第二轮就变了）。"""
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"body_type": ["suv"], "usage": ["通勤"]})
    profile = UserProfile()
    restore_into_profile(db_session, identity, profile)
    first = (list(profile.body_type), list(profile.usage))
    restore_into_profile(db_session, identity, profile)
    assert (list(profile.body_type), list(profile.usage)) == first


# ── 忘记 ────────────────────────────────────────────────────────────────────


def test_forget_removes_the_key(db_session: Session, durable_on):
    identity, _ = _identity(db_session)
    persist_hints(db_session, identity, {"usage": ["通勤"]})
    assert forget(db_session, identity, "usage") is True
    assert "usage" not in memories_to_hints(db_session, identity)


# ── 接口 ────────────────────────────────────────────────────────────────────


def test_identity_endpoint_issues_a_token_once(client: TestClient):
    resp = client.post("/api/v1/agent/identity")
    assert resp.status_code == 201
    data = resp.json()
    assert data["created"] is True
    assert data["token"]

    # 再带 token 调用：应返回**同一个**身份，不凭空多一个
    again = client.post(
        "/api/v1/agent/identity",
        headers={"Authorization": f"Bearer {data['token']}"},
    )
    assert again.json()["identity_id"] == data["identity_id"]
    assert again.json()["created"] is False
    assert again.json()["token"] == ""


def test_preferences_without_identity_returns_empty(client: TestClient):
    """前端可以无条件调用：没领身份就返回空列表，而不是 401。"""
    resp = client.get("/api/v1/agent/preferences")
    assert resp.status_code == 200
    assert resp.json() == []


def test_preferences_with_garbage_token_returns_empty(client: TestClient):
    resp = client.get(
        "/api/v1/agent/preferences", headers={"Authorization": "Bearer nonsense"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_delete_preference_is_scoped_to_the_identity(
    client: TestClient, db_session: Session, durable_on
):
    """别人的记忆删不掉（404，不是 403——不泄露「这条存在但不属于你」）。"""
    owner = client.post("/api/v1/agent/identity").json()
    owner_headers = {"Authorization": f"Bearer {owner['token']}"}
    client.post(
        "/api/v1/agent/sessions",  # 会话存在即可（消息接口会校验）
    )
    memory_id = _seed_one_memory(client, db_session, owner_headers)

    other = client.post("/api/v1/agent/identity").json()
    other_headers = {"Authorization": f"Bearer {other['token']}"}
    resp = client.delete(f"/api/v1/agent/preferences/{memory_id}", headers=other_headers)
    assert resp.status_code == 404

    resp = client.delete(f"/api/v1/agent/preferences/{memory_id}", headers=owner_headers)
    assert resp.status_code == 204


def _seed_one_memory(
    client: TestClient, db_session: Session, headers: dict[str, str]
) -> int:
    """借接口写一条记忆：发一句带约束的话，返回那条记忆的 id。"""
    session_id = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"message": "预算12万"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    listed = client.get("/api/v1/agent/preferences", headers=headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert rows, "前提：这一轮确实写进了一条记忆"
    return int(rows[0]["id"])


# ── 端到端：跨会话真的省掉重复提问 ──────────────────────────────────────────


def test_second_session_does_not_ask_the_budget_again(
    client: TestClient, db_session: Session, durable_on
):
    """真后端 + 真结构断言：第一段对话说了预算，**第二段对话不该再问**。"""
    _seed_agent_data(db_session)
    ident = client.post("/api/v1/agent/identity").json()
    headers = {"Authorization": f"Bearer {ident['token']}"}

    first_session = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{first_session}/messages",
        json={"message": "预算12万"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # 记忆必须**真的落库**：`app/common/database.py::get_session` 不提交，
    # 请求结束即回滚。只 flush 不 commit 等于没写——回滚一次就能看出来。
    db_session.rollback()
    assert db_session.query(PreferenceMemory).count() == 1

    second_session = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{second_session}/messages",
        json={"message": "我想买台车"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["need_clarification"] is True
    # 预算已被记忆带回，缺的不再是它
    assert "budget" not in body["clarification"]["missing"]


def test_without_identity_the_budget_is_asked_again(
    client: TestClient, db_session: Session, durable_on
):
    """对照组：不带身份 → 没有记忆 → 该问还得问（证明上一条不是因为「问了就不问」）。"""
    _seed_agent_data(db_session)

    first_session = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{first_session}/messages",
        json={"message": "预算12万"},
    )
    assert resp.status_code == 200, resp.text

    second_session = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{second_session}/messages",
        json={"message": "我想买台车"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["need_clarification"] is True
    assert body["clarification"]["missing"] == ["budget"]


def test_gate_off_keeps_the_old_behaviour(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """闸门关着时，行为与接入前**逐位相同**：还是问预算。"""
    monkeypatch.setenv(MODE_ENV, "off")
    _seed_agent_data(db_session)
    ident = client.post("/api/v1/agent/identity").json()
    headers = {"Authorization": f"Bearer {ident['token']}"}

    first_session = _create_session(client)
    client.post(
        f"/api/v1/agent/sessions/{first_session}/messages",
        json={"message": "预算12万"},
        headers=headers,
    )
    second_session = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{second_session}/messages",
        json={"message": "我想买台车"},
        headers=headers,
    )
    body = resp.json()
    assert body["clarification"]["missing"] == ["budget"]


def test_gate_off_does_not_even_resolve_the_identity(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """`off` = **完全不介入**：连身份都不查——每次请求少一次 SELECT。"""
    monkeypatch.setenv(MODE_ENV, "off")
    _seed_agent_data(db_session)
    calls: list[str | None] = []
    original = agent_router._resolve_identity

    def spy(db: Session, authorization: str | None):
        calls.append(authorization)
        return original(db, authorization)

    monkeypatch.setattr(agent_router, "_resolve_identity", spy)
    session_id = _create_session(client)
    resp = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages", json={"message": "预算12万"}
    )
    assert resp.status_code == 200, resp.text
    assert calls == []


# ── 迁移 ────────────────────────────────────────────────────────────────────
#
# 为什么必须测迁移：本地开发走 `Base.metadata.create_all`（按**模型**建表），
# 生产走 `alembic upgrade head`（按**迁移文件**建表）。两条路只要有一处不一致，
# 缺陷就**只在生产暴露**——本地全绿、上线缺列。


def _load_migration():
    from pathlib import Path

    p = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "d3e8f1a4b7c2_add_anonymous_identities_and_preference_memories.py"
    )
    spec = importlib.util.spec_from_file_location("mig_durable_prefs", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _migrated_engine(tmp_path) -> sa.Engine:
    """在 SQLite 上实跑一遍迁移（生产是 PG，两个都得能过）。"""
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with eng.begin() as c:
        # 迁移里的外键指向这两张表，先建出最小骨架
        c.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        c.execute(sa.text("CREATE TABLE agent_turn_logs (id INTEGER PRIMARY KEY)"))
    with eng.begin() as c, Operations.context(MigrationContext.configure(c)):
        _load_migration().upgrade()
    return eng


def test_migration_upgrades_and_downgrades_on_sqlite(tmp_path):
    """upgrade / downgrade 的 op 在 SQLite 上实跑一遍。"""
    eng = _migrated_engine(tmp_path)
    insp = sa.inspect(eng)
    tables = insp.get_table_names()
    assert "anonymous_identities" in tables
    assert "preference_memories" in tables
    cols = {c["name"] for c in insp.get_columns("preference_memories")}
    assert {"identity_id", "category", "key", "value", "confirmed"} <= cols

    with eng.begin() as c, Operations.context(MigrationContext.configure(c)):
        _load_migration().downgrade()
    tables2 = sa.inspect(eng).get_table_names()
    assert "anonymous_identities" not in tables2
    assert "preference_memories" not in tables2


def test_migrated_schema_matches_the_models(tmp_path):
    """迁移出来的列必须与模型**逐个对齐**——生产按迁移建表，本地按模型建表。

    对不齐的列只会在生产暴露（本地 `create_all` 一切正常）。
    """
    eng = _migrated_engine(tmp_path)
    insp = sa.inspect(eng)
    for table, model in (
        ("anonymous_identities", AnonymousIdentity),
        ("preference_memories", PreferenceMemory),
    ):
        migrated = {c["name"] for c in insp.get_columns(table)}
        declared = set(model.__table__.c.keys())
        assert migrated == declared, (
            f"{table} 的迁移与模型不一致："
            f"迁移缺 {sorted(declared - migrated)}，迁移多 {sorted(migrated - declared)}"
        )
