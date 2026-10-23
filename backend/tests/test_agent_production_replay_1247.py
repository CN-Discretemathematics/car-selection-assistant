# -*- coding: utf-8 -*-
"""12:47 生产实测的原始缺陷，在 main 上是否已修（2026-10-23 复核）

## 这份文件的作用

`test_agent_answered_clarification.py` 里的 5 条在 main 上**也全过** ⇒
它们断言的是 #93 已经修好的行为，**不是**缺陷复现。

⇒ 需要一份**能在 main 上变红**的用例，才算真正钉住缺陷。
本文件就是那份：逐条复刻 2026-10-23 12:47 生产实测的 5 轮对话。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.seed import make_brand, make_sales, make_series, make_source, make_variant, make_year


def _seed(db_session: Session) -> None:
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    suv = make_series(
        db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source
    )
    sedan = make_series(
        db_session, brand, name="通勤家轿", body_type="sedan", energy_types=("BEV",), source=source
    )
    year_suv, year_sedan = make_year(db_session, suv), make_year(db_session, sedan)
    v_suv = make_variant(
        db_session, suv, year_suv, config_version="标准版", energy_type="BEV",
        price_cny="129800", facts=[("座位数", "seats", "5", "座", None)], source=source,
    )
    v_sedan = make_variant(
        db_session, sedan, year_sedan, config_version="标准版", energy_type="BEV",
        price_cny="99800", facts=[("座位数", "seats", "5", "座", None)], source=source,
    )
    make_sales(db_session, v_suv, month="2026-09", count=2000)
    make_sales(db_session, v_sedan, month="2026-09", count=1500)
    db_session.commit()


def _create_session(client: TestClient) -> str:
    resp = client.post("/api/v1/agent/sessions")
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _send(client: TestClient, session_id: str, message: str) -> dict:
    resp = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages", json={"message": message}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _asked_missing(payload: dict) -> list[str]:
    """这一轮问了哪些维度（追问卡与随推荐给出的尾巴追问都算）。"""
    if payload.get("need_clarification") and payload.get("clarification"):
        return list(payload["clarification"].get("missing") or [])
    if payload.get("followup"):
        return list(payload["followup"].get("missing") or [])
    return []


def _has_cars(payload: dict) -> bool:
    return bool(payload.get("recommended_variants") or payload.get("recommended_series_ids"))


def test_production_12_47_replay_usage_asked_at_most_once(
    db_session: Session, client: TestClient
):
    """**逐轮复刻 2026-10-23 12:47 的生产实测**：用途最多被问一次。

    原对话（用户是真人）：

    | 轮 | 用户说 | 原行为 |
    | --- | --- | --- |
    | 0 | 「15万预算有什么能选的」 | 追问卡（正文空）——明确要清单却一款车没拿到 |
    | 1 | 「都可以」 | 问人数（正常） |
    | 2 | 「2人」 | ❗**又问用途**（用户第 1 轮已答「都可以」） |
    | 3 | 「都可以」 | ❗「没法凭预算直接列推荐清单」 |
    | 4 | 「suv」 | ❗**第三次问用途** |

    ⇒ 缺陷 = **同一个问题连问三遍**。

    ## 与姊妹文件的关系（2026-10-24 审计澄清）

    `test_agent_vague_user_guidance.py::test_waived_usage_is_never_asked_twice`
    跑**同一段 5 轮脚本**，判据「用途最多被问一次」——**本条更强**（`== 0`）。

    两份并存的理由：vague 那条盯「每轮都在给车」（逐轮断言 `need_clarification is False`
    + `recommended_variants` 非空），本条盯「**第 2/4 轮不许再问用途**」这一条缺陷本身。
    ⚠️ 审计建议合并成一条，**不采纳**：合并后会让其中一份文件失去自己的判据维度。
    但两边的**上界**必须一致 ⇒ vague 那条已同步收紧为「第 0 轮可问、第 1 轮起不许」。
    """
    _seed(db_session)
    session_id = _create_session(client)

    _send(client, session_id, "15万预算有什么能选的")
    _send(client, session_id, "都可以")
    second = _send(client, session_id, "2人")
    _send(client, session_id, "都可以")
    fourth = _send(client, session_id, "suv")

    usage_asks = sum(1 for m in _asked_missing(second) + _asked_missing(fourth) if m == "usage")
    assert usage_asks == 0, (
        f"用途被问了 {usage_asks} 次——用户第 1 轮已说「都可以」（放弃用途）"
        f"，同一个问题不许问第二遍。second={second} fourth={fourth}"
    )


def test_production_12_47_replay_first_turn_gives_cars(
    db_session: Session, client: TestClient
):
    """**逐轮复刻**：第 0 轮「15万预算有什么能选的」必须**给得出车**。

    原行为：追问卡（正文空）⇒ 用户明确要清单，却一款车都没拿到。
    """
    _seed(db_session)
    session_id = _create_session(client)

    first = _send(client, session_id, "15万预算有什么能选的")
    assert _has_cars(first), f"用户明确要清单，第 0 轮就该给车：{first}"


def test_waived_dimension_is_never_asked_again_in_production_flow(
    db_session: Session, client: TestClient
):
    """**同一个问题不许问第二遍**（生产实测：用途被问了三遍）。

    ## 构造要点

    `waived_dims` 只在**引擎此刻正追问该项**时才写入（`engine.py` 的
    `missing in profile.unknowns` 分支）⇒ 用例必须让引擎**真的进入追问态**。

    而 #93 之后「明确要清单」**不再发追问卡**（改用 `followup` 尾巴问）
    ⇒ 想进追问态，第 0 轮就**不能说**「有什么能选」——只给预算即可。

    | 轮 | 用户说 | 引擎应处于 |
    | --- | --- | --- |
    | 0 | 「预算12万」 | 追问态（问用途） |
    | 1 | 「都可以」 | 放弃用途 ⇒ 记进 `waived_dims` |
    | 2 | 「再说说」 | 用途**不得**再被问（该问人数） |
    """
    _seed(db_session)
    session_id = _create_session(client)

    # 第 0 轮：只给预算（**不**说「有什么能选」）⇒ 引擎进入追问态
    first = _send(client, session_id, "预算12万")
    assert "usage" in _asked_missing(first), f"第 0 轮应问用途：{first}"

    # 第 1 轮：用户放弃用途 ⇒ 应记进 waived_dims
    second = _send(client, session_id, "都可以")
    # 第 2 轮：用途**不得**再被问
    third = _send(client, session_id, "再说说")
    assert "usage" not in _asked_missing(third), (
        f"用户已放弃用途，却又问用途：{third}"
    )
    assert "usage" not in _asked_missing(second), (
        f"放弃的那一轮就不该再问用途：{second}"
    )


def test_production_12_47_replay_give_up_still_gets_cars(
    db_session: Session, client: TestClient
):
    """**逐轮复刻**：第 3 轮再说「都可以」时，仍必须给得出车。

    原行为：模型说「没法凭预算直接列一份推荐清单」——库里明明有 870+ 款在售。
    """
    _seed(db_session)
    session_id = _create_session(client)

    _send(client, session_id, "15万预算有什么能选的")
    _send(client, session_id, "都可以")
    _send(client, session_id, "2人")
    third = _send(client, session_id, "都可以")

    assert _has_cars(third), f"用户放弃用途后仍应给得出车：{third}"
