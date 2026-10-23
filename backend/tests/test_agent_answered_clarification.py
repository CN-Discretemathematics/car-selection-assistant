# -*- coding: utf-8 -*-
"""用户**已经答了**被追问的那一项，引擎却还在问它（2026-10-23 14:50 生产实测）

## 来源：生产**真实用户会话**（不是构造的探针）

2026-10-23 14:50，生产上有一段 3 轮真实对话（`agent_turn_logs` 里 session
`ad80af79…`）：

| 轮 | 用户说 | 助手回答 | 问题 |
| --- | --- | --- | --- |
| 0 | 「预算12万油车有什么推荐」 | 追问卡（正文空） | 明确要推荐，却一款车都没拿到 |
| 1 | 「上下班通勤」（**答用途**） | 追问卡（正文空） | ❗**用途已经答了，还在问人数**（可接受）——但正文空、用户看不到任何进展 |
| 2 | 「1~2人」 | 给出了 5 款车 | 这轮正常 |

第 1 轮是本文件的靶心：**用户明确回答了「主要用途」**（上一轮问的就是用途），
引擎却发了追问卡。

## 根因

`engine.respond` 的追问卡在 **画像推进之前**发出：
`next_clarification(profile)` 读到的 `profile` 里**还没有本轮解析出的用途**
⇒ 用途仍算缺失 ⇒ 追问卡照发。

⇒ 用户明明答了，系统装作没听见 ⇒ 「说了等于没说」。
（#93 修的是「用户放弃某一项」（「都可以」），**没有**修「用户回答了某一项」。）

## 本文件的判据

- 用户回答了正在被追问的那一项 ⇒ **不许再发那张追问卡**；
- 即便仍要问别的（人数），也**不许装作用途没被回答**；
- 不回归：用户**没有**回答时，追问卡照发。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.schemas import Budget, UserProfile
from tests.seed import make_brand, make_sales, make_series, make_source, make_variant, make_year


def _seed(db_session: Session) -> None:
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    suv = make_series(
        db_session, brand, name="家用SUV", body_type="suv", energy_types=("ICE",), source=source
    )
    year = make_year(db_session, suv)
    variant = make_variant(
        db_session,
        suv,
        year,
        config_version="标准版",
        energy_type="ICE",
        price_cny="129800",
        facts=[("座位数", "seats", "5", "座", None)],
        source=source,
    )
    make_sales(db_session, variant, month="2026-09", count=1234)
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
    """这一轮问了哪些维度。

    ⚠️ 两个位置都算：追问卡（`clarification`）与**随推荐一起给**的尾巴追问
    （`followup`）——#93 之后，用户明确要清单时**不再发追问卡挡住结果**，
    缺口改用 `followup` 尾巴问 ⇒ 只看 `clarification` 会漏判。
    """
    if payload.get("need_clarification") and payload.get("clarification"):
        return list(payload["clarification"].get("missing") or [])
    if payload.get("followup"):
        return list(payload["followup"].get("missing") or [])
    return []


def test_answering_the_pending_question_is_not_asked_again(db_session: Session, client: TestClient):
    """**核心判据**：用户答了正在被追问的那一项 ⇒ 不许再问它。

    复现 2026-10-23 14:50 的生产实测：第 0 轮问用途，第 1 轮用户答「上下班通勤」，
    引擎却仍把用途当缺失（追问卡正文空，用户看不到任何进展）。
    """
    _seed(db_session)
    session_id = _create_session(client)

    # 第 0 轮：给预算，要推荐 ⇒ 引擎问的是「用途」
    first = _send(client, session_id, "预算12万油车有什么推荐")
    assert "usage" in _asked_missing(first), f"第 0 轮应问用途：{first}"

    # 第 1 轮：用户**明确回答用途**
    second = _send(client, session_id, "上下班通勤")

    # ❗ 用途已被回答 ⇒ 绝不能**再问用途**
    assert "usage" not in _asked_missing(second), f"用户已答用途，却又被问用途：{second}"


def test_answering_pending_question_still_allows_asking_others(db_session: Session, client: TestClient):
    """答了用途后**仍可问人数**——但不能因此「装作用途没被回答」。

    真人销售会问细节，但不会假装没听见上一句。
    """
    _seed(db_session)
    session_id = _create_session(client)

    _send(client, session_id, "预算12万油车有什么推荐")
    second = _send(client, session_id, "上下班通勤")

    missing = _asked_missing(second)
    assert missing != ["usage"], f"不该再问用途：{second}"
    if missing:
        # 若还在追问，**追问文案本身**必须体现已收到的用途
        # （前端渲染的是 clarification.question / followup.question，见 AgentChat.tsx
        #   的 ClarificationChips ⇒ 正文在这两个字段里，不在 explanation）
        question = ""
        if second.get("clarification"):
            question = second["clarification"].get("question") or ""
        elif second.get("followup"):
            question = second["followup"].get("question") or ""
        assert question.strip(), f"追问文案不能为空：{second}"
        assert "通勤" in question, f"追问文案应体现已收到的用途（已记住…通勤）：{second}"


def test_unanswered_question_still_asks(db_session: Session, client: TestClient):
    """**不回归**：用户**没有**回答被追问的那一项 ⇒ 仍要问。"""
    _seed(db_session)
    session_id = _create_session(client)

    first = _send(client, session_id, "预算12万油车有什么推荐")
    assert "usage" in _asked_missing(first), f"没回答时应问用途：{first}"


def test_answered_usage_is_kept_in_profile(db_session: Session, client: TestClient):
    """画像层：用户答的用途**要真的落进画像**（否则下一轮又当没答过）。"""
    _seed(db_session)
    session_id = _create_session(client)

    _send(client, session_id, "预算12万油车有什么推荐")
    second = _send(client, session_id, "上下班通勤")

    # 本轮返回的 filters 里应已带上用途对应的过滤（能源=ICE 来自第 0 轮，
    # 用途不在 filters 里 ⇒ 改查「不再问用途」这个可观测后果）
    assert "usage" not in _asked_missing(second), second


def test_next_clarification_respects_answered_usage():
    """单元层：`next_clarification` 在画像**已含**用途时不得再问用途。

    这是上面端到端用例的判据内核——若这一层问用途，端到端必然问。
    """
    profile = UserProfile()
    profile.budget = Budget(max=120000.0, currency="CNY", type="official_msrp")
    profile.usage = ["通勤"]
    from app.agent.engine import next_clarification

    clarification = next_clarification(profile)
    if clarification is not None:
        assert "usage" not in clarification.missing, clarification
