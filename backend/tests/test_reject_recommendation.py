"""「不喜欢这台」→ 后续不再推荐这一款（计划「拒绝后不再推」场景，2026-10-21）。

## 语义边界

- 只认**带指示词**的否定：「不喜欢这台」「不要这辆」「别推它」；
- 裸的「不要」（「不要 SUV」「不要电车」）是**硬约束**，走 `avoid` / 品牌排除
  那条路——两者混在一起会把一整类车误伤成一款车；
- 被拒绝的款型**只在会话内**被排除（跨会话要靠反馈表 + 影子调参，Phase 2 的另一半）。

## 守卫

- 端到端：拒绝后下一轮的候选里**没有**那一款；
- 前提断言：被拒的那款**本来就在**候选里（否则测的是空气）；
- 反向：硬约束的「不要 SUV」**不得**被当成拒绝（一整类车不能被误伤成一款）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.agent.tools as tools_mod
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_three_cars(db_session: Session) -> list[int]:
    """三款车（用来让「最后一款」有确定所指）。"""
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    ids: list[int] = []
    for name in ("甲车系", "乙车系", "丙车系"):
        series = make_series(
            db_session, brand, name=name, body_type="sedan",
            energy_types=("BEV",), source=source,
        )
        year = make_year(db_session, series)
        variant = make_variant(
            db_session, series, year, config_version="标准版", energy_type="BEV",
            price_cny="150000", source=source,
        )
        ids.append(variant.id)
    db_session.commit()
    return ids


def _create_session(client: TestClient) -> str:
    resp = client.post("/api/v1/agent/sessions")
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _ask(client: TestClient, session_id: str, message: str) -> dict:
    resp = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages", json={"message": message}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_rejected_variant_is_not_recommended_again(client: TestClient, db_session: Session):
    """拒绝后下一轮**不再**推它。"""
    _seed_three_cars(db_session)
    sid = _create_session(client)
    first = _ask(client, sid, "预算20万，通勤，两三个人")
    recommended = [v["variant_id"] for v in first.get("recommended_variants", [])]
    assert recommended, "前提：这一轮应当有推荐"
    rejected = recommended[-1]

    second = _ask(client, sid, "不喜欢这台")
    after = [v["variant_id"] for v in second.get("recommended_variants", [])]
    assert rejected not in after, f"被拒绝的款型又出现了：{after}"
    assert any("已排除" in r for r in second.get("reasons", [])), (
        f"排除必须对用户可见（口径变化要说出来）：{second.get('reasons')}"
    )


def test_rejection_does_not_hurt_the_other_cars(client: TestClient, db_session: Session):
    """只排除**那一款**——其余推荐不受影响。"""
    _seed_three_cars(db_session)
    sid = _create_session(client)
    first = _ask(client, sid, "预算20万，通勤，两三个人")
    recommended = [v["variant_id"] for v in first.get("recommended_variants", [])]
    assert len(recommended) >= 2, "前提：这一轮应当有多款推荐"

    second = _ask(client, sid, "不喜欢这台")
    after = [v["variant_id"] for v in second.get("recommended_variants", [])]
    assert set(recommended[:-1]) <= set(after), (
        f"除了被拒的那款，其余推荐不该变：{recommended} → {after}"
    )


def test_hard_constraint_is_not_mistaken_for_a_rejection(
    client: TestClient, db_session: Session
):
    """反向：「不要 SUV」是**硬约束**，不是拒绝某一台车。

    混在一起的后果：一整类车被误伤成一款车。
    """
    _seed_three_cars(db_session)
    sid = _create_session(client)
    _ask(client, sid, "预算20万，通勤，两三个人")
    resp = _ask(client, sid, "不要这类的")
    # 「不要这类的」含指示词但不是「这台/这辆」→ 不是拒绝 → 不该出现「已排除」
    assert not any("已排除" in r for r in resp.get("reasons", [])), (
        f"硬约束被当成了拒绝某一台：{resp.get('reasons')}"
    )


def test_rejection_without_a_prior_recommendation_is_honest(
    client: TestClient, db_session: Session
):
    """还没推荐过就说不喜欢 → **如实说**，不假装执行了。"""
    _seed_three_cars(db_session)
    sid = _create_session(client)
    resp = _ask(client, sid, "不喜欢这台")
    reasons = " ".join(resp.get("reasons", []))
    assert resp.get("need_clarification") or "还没推荐过" in reasons, (
        f"没推荐过就该如实说（要么走追问，要么说明白）：{resp}"
    )


def test_rejected_variant_is_filtered_in_sql(db_session: Session):
    """被拒的款型**不进 SQL 候选集**（`candidates_scanned` 就不含它）。

    为什么单独钉这一条：Python 侧的兜底也能把车挡掉，于是「只留兜底、去掉下推」
    这种半修**端到端看不出来**——但全库扫描的候选集仍然包含它（多扫一遍）。
    `candidates_scanned` 是 SQL 命中数，正好能区分这两件事 ✓。
    """
    from app.agent.schemas import Budget, UserProfile
    from app.agent.tools import recommendation_tool

    ids = _seed_three_cars(db_session)
    profile = UserProfile(budget=Budget(max=200000), usage=["通勤"])
    base = recommendation_tool(db_session, profile)
    assert base["candidates_scanned"] == 3, base["candidates_scanned"]

    profile.rejected_variant_ids = [ids[0]]
    after = recommendation_tool(db_session, profile)
    assert after["candidates_scanned"] == 2, (
        f"被拒的款型应在下推阶段就被排除（SQL 侧），实际扫了 "
        f"{after['candidates_scanned']} 个"
    )
    assert ids[0] not in [v["variant_id"] for v in after["variants"]]


def test_rejection_is_filtered_in_both_sql_and_python_side():
    """两处口径必须一致：**SQL 下推 + Python 兜底**（少一处 = 半修）。

    端到端**看不出**「只留兜底、去掉下推」或反过来——两处都能把车挡掉；
    但「只留兜底」意味着全库扫描时仍把被拒的车捞进候选集（白扫一遍）。

    故这里用**源码级**断言把两处都钉住（本仓其它源码级契约测试的同一范式）。
    """
    tree = ast.parse(Path(tools_mod.__file__).read_text(encoding="utf-8"))
    sql_pushdown = False
    python_fallback = False
    for node in ast.walk(tree):
        # SQL 侧：VehicleVariant.id.in_(profile.rejected_variant_ids)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "in_"
            and any(
                isinstance(a, ast.Attribute) and a.attr == "rejected_variant_ids"
                for a in node.args
            )
        ):
            sql_pushdown = True
        # Python 侧：variant.id in profile.rejected_variant_ids
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "id"
            and any(
                isinstance(c, ast.Attribute) and c.attr == "rejected_variant_ids"
                for c in node.comparators
            )
        ):
            python_fallback = True
    assert sql_pushdown, "SQL 侧必须下推排除（否则全库扫描仍把被拒的车捞进候选集）"
    assert python_fallback, "Python 侧必须有同口径兜底（本仓惯例：下推 + 兜底）"
