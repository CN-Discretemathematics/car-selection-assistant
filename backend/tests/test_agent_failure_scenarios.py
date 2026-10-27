"""智能导购的**失败场景**集（2026-10-21 TDD）

## 这组用例在干什么

用户要求：「模拟几个会出错的场景，再去修复」——
所以这里**先写会红的用例**（每个都是一种真实会出错的用户可见故障），
再按红的结果去修。已知的现状：

| 场景 | 故障 | 修之前 |
| --- | --- | --- |
| S1 解释路径编数字 | 解释里出现**库外数字** | 解释路径（`_explain`）**没有任何数字守卫** ⇒ 编的数字直接进用户屏幕 |
| S2 身份串台 | A 的记忆影响 B | 身份隔离 |
| S3 撤回后仍生效 | 删掉的记忆还回填 | 撤回权 |
| S4 同轮排除不生效 | 当轮说「不要 X」却仍推 X | 会话内排除 |
| S5 空候选编车 | 没有合适的车却给出车 | 拒答诚实性 |

每条用例都尽量带**前提断言**（否则就是「测空气」）：
例如 S1 先确认库里的真实数字与编造的那个不同，S5 先确认确实没有候选。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.preferences import MODE_ENV
from tests.seed import (
    make_brand,
    make_series,
    make_source,
    make_variant,
    make_year,
)
from tests.test_tool_loop import _client_with_llm, _FakeLLM


@pytest.fixture()
def durable_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开持久化闸门（默认 off，测试必须显式打开）。"""
    monkeypatch.setenv(MODE_ENV, "on")


def _seed_cars(db_session: Session) -> None:
    """两个品牌各一款车（一个 15 万 BEV、一个 20 万 ICE），带座位数事实。"""
    source = make_source(db_session, name="失败场景来源")
    for brand_name, series_name, body, energy, price in (
        ("大众", "迈腾", "sedan", "ICE", "200000"),
        ("测试比亚迪", "汉L", "sedan", "PHEV", "150000"),
    ):
        brand = make_brand(db_session, name=brand_name, source=source)
        series = make_series(
            db_session, brand, name=series_name, body_type=body,
            energy_types=(energy,), source=source,
        )
        year = make_year(db_session, series)
        make_variant(
            db_session, series, year, config_version="标准版", energy_type=energy,
            price_cny=price,
            facts=[("座位数", "seats", "5", "座", None)],
            source=source,
        )
    db_session.commit()


def _ask(client: TestClient, message: str, token: str | None = None, session_id: str | None = None) -> dict:
    """问一轮。

    ⚠️ `session_id` 必须复用同一个会话：引擎的画像是**会话内累积**的，
    追问补齐（用途 → 预算 → …）也只在同一会话里才问得完。
    每轮新开会话 ⇒ 画像从零开始 ⇒ 引擎永远从头追问（第一版就栽在这里）。
    """
    sid = session_id or client.post("/api/v1/agent/sessions").json()["session_id"]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.post(
        f"/api/v1/agent/sessions/{sid}/messages", json={"message": message}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _identity(client: TestClient) -> tuple[dict, str]:
    ident = client.post("/api/v1/agent/identity").json()
    return ident, ident["token"]


def _brands(answer: dict) -> set[str]:
    return {
        str(v.get("brand_name") or "")
        for v in (answer.get("recommended_variants") or [])
    }


# ── S1 解释路径编数字 ────────────────────────────────────────────────────────


def test_explain_path_must_not_invent_numbers(client: TestClient, db_session: Session):
    """S1：解释里出现**库外数字** ⇒ 必须被拦下（用户看到的是编出来的续航）。

    场景：假 LLM 在解释里写「续航 802 公里」——库里根本没有这个数。
    修之前：解释路径（`engine.py::_explain`）**没有任何数字守卫**
    ⇒ 编造的数字直接进用户屏幕（工具循环有守卫，解释路径没有）。

    判据：**只守数字，不守名字**；允许集 = 候选款型 + 检索证据 + 画像（与工具循环同口径）。
    """
    _seed_cars(db_session)
    fake = _FakeLLM([
        {"content": "汉L 续航 802 公里，电池 82.6 度，综合续航优秀。"},
        {"content": "汉L 官方指导价 15 万元。"},
    ])
    restore = _client_with_llm(client, db_session, fake)
    try:
        # 走**推荐**路径（结构化需求句，不是「有哪些车」那类工具问答）
        answer = _ask(client, "预算15万，家里5口人，想要家用轿车")
    finally:
        restore()
    text = answer.get("explanation") or ""
    assert "802" not in text, (
        f"解释里出现了库外数字 802（编造）⇒ 直接进用户屏幕。实际解释：{text[:200]}"
    )


# ── S2 身份串台 ────────────────────────────────────────────────────────────


def test_memory_of_one_identity_does_not_leak_to_another(
    client: TestClient, db_session: Session, durable_on
):
    """S2：A 记住的「不要大众」**不得**影响 B。

    前提断言：没有记忆时「大众」**确实**会被推荐（否则本用例测的是空气）。
    """
    _seed_cars(db_session)
    _ident_a, token_a = _identity(client)

    # 前提：没有记忆时大众在榜
    plain = _ask(client, "预算20万，5口人通勤")
    assert "大众" in _brands(plain), f"前提不成立：{_brands(plain)}"

    # A 记住「不要大众」
    _ask(client, "预算20万，5口人通勤，不要大众", token_a)

    # B（另一个身份）：大众**仍应**在榜
    _ident_b, token_b = _identity(client)
    other = _ask(client, "预算20万，5口人通勤", token_b)
    assert "大众" in _brands(other), (
        f"另一个身份被别人的记忆影响了（串台）：{_brands(other)}"
    )


# ── S3 撤回记忆后仍生效 ──────────────────────────────────────────────────────


def test_forgotten_memory_is_not_restored_in_the_next_session(
    client: TestClient, db_session: Session, durable_on
):
    """S3：用户删掉一条记忆后，下一段对话**不得**再回填它。

    前提断言：删之前它**确实**在生效（否则本用例测的是空气）。
    """
    _seed_cars(db_session)
    _ident, token = _identity(client)

    _ask(client, "预算20万，5口人通勤，不要大众", token)
    rows = client.get("/api/v1/agent/preferences", headers={"Authorization": f"Bearer {token}"}).json()
    brand_rows = [r for r in rows if r["key"] == "brand_exclude"]
    assert brand_rows, "前提不成立：排除项根本没被记住"

    # 删掉它
    resp = client.delete(
        f"/api/v1/agent/preferences/{brand_rows[0]['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204, resp.text

    # 下一段对话：大众**应当**回到候选里（记忆已撤）
    after = _ask(client, "预算20万，5口人通勤", token)
    assert "大众" in _brands(after), (
        f"记忆删掉了却仍生效（撤回权失效）：{_brands(after)}"
    )


# ── S4 同轮排除不生效 ────────────────────────────────────────────────────────


def test_exclusion_in_the_same_turn_takes_effect_immediately(
    client: TestClient, db_session: Session
):
    """S4：用户**当轮**说「不要 X」⇒ 当轮的推荐里就不该有 X。

    前提断言：没有排除项时 X **确实**会被推荐（否则测的是空气）。
    """
    _seed_cars(db_session)
    plain = _ask(client, "预算20万，5口人通勤")
    assert "大众" in _brands(plain), f"前提不成立：{_brands(plain)}"

    with_exclusion = _ask(client, "预算20万，5口人通勤，不要大众")
    assert "大众" not in _brands(with_exclusion), (
        f"当轮说的排除项没生效：{_brands(with_exclusion)}"
    )


# ── S5 空候选编车 ────────────────────────────────────────────────────────────


def test_no_candidate_means_say_so_not_invent_one(
    client: TestClient, db_session: Session
):
    """S5：库里没有满足条件的车 ⇒ 必须说「没有合适的车」，**不得**给出任何车。

    ⚠️ 判据要**等追问补齐条件之后**再下：引擎第一轮合理地反问「主要用来跑什么？」
    （缺 usage），此时**还没**判定无候选 ⇒ 直接断言「必须说没有」是**过早**的
    （第一版就栽在这里）。所以这里问两轮：
    第一轮补齐用途，第二轮条件齐了仍无候选 ⇒ 此时必须明说。
    """
    _seed_cars(db_session)
    # 引擎会**分轮**把缺失的条件问齐（用途 → 预算 → …）⇒ 同一会话里跟着补齐，最多 4 轮
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    answer = _ask(client, "预算80万，家里9口人，要7座MPV", session_id=sid)
    replies = {
        "usage": "家庭出行",
        "budget": "预算80万",
        "passengers": "9口人",
        "body_type": "MPV",
        "energy_preference": "燃油",
    }
    for _ in range(4):
        assert not (answer.get("recommended_variants") or []), (
            f"条件还没问齐却给出了推荐 ⇒ 在编车：{answer.get('recommended_variants')}"
        )
        missing = (answer.get("clarification") or {}).get("missing") or []
        if not missing:
            break
        answer = _ask(client, replies.get(missing[0], "按刚才说的"), session_id=sid)
    assert not (answer.get("recommended_variants") or []), (
        f"条件齐了仍无候选，却给出了推荐 ⇒ 在编车：{answer.get('recommended_variants')}"
    )
    text = (answer.get("explanation") or "") + str(answer.get("clarification") or "")
    assert ("没有" in text) or ("放宽" in text) or ("合适" in text), (
        f"条件齐了仍无候选时应当明说「没有合适的车 / 可以放宽」，实际文案：{text[:200]}"
    )
