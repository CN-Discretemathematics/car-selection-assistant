# -*- coding: utf-8 -*-
"""用户**回答了**正在被追问的那一项 ⇒ 下一轮必须**用上**它（2026-10-24 审计改写）

## ⚠️ 本文件经过一次审计改写（初版是假绿）

初版把根因写成「追问卡在**画像推进之前**发出」——**那是错的**，已由生产实测纠正。

**真实机制**（2026-10-23 生产实测）：

- 顺序是 **画像推进 → 追问**（`engine.py:1301` `advance_profile`
  → `turn.py:443` `merge_profile` 把本轮线索并进画像 → `engine.py:1494`
  `next_clarification(profile)`）；
- 所以「用户答了用途 ⇒ 用途已进画像」是**成立**的
  （实测 `next_clarification` 返回 `missing=['passengers']`）；
- 真正的风险在**别处**：#93 之后「用户明确要清单」**不再发追问卡**
  （改用 `followup` 尾巴问，`engine.py:1553`）⇒
  「答了某项就不许再问它」这个判据**由 `followup` 承担**，不是由追问卡承担。

### 初版为什么是假绿（审计实测复现）

初版判据是「用户答了用途 ⇒ 不许**再问**用途」。实测发现：把用途**整个解析不出来**
（`USAGE_HINTS` 的「通勤」「上下班」两键都改掉 ⇒ `extract_hints('上下班通勤')` 返回 `{}`），
**这 4 条用例照样全绿**。

原因：用途没解析出来时，引擎**根本不会走到「问用途」那一步**
（第 0 轮问的就是用途）⇒ 「不再问用途」在两种情况下都成立 ⇒ 判据**不区分**它们。

⇒ 改用**能区分**的强判据：**用户答了用途 ⇒ 下一轮的追问文案必须回显「用途通勤」**
（回执来自 `_priority_phrase`，读的是 `profile.usage`）。
用途没进画像 ⇒ 回执里就没有「用途」⇒ 用例变红。

## 本文件的判据

1. **回显**：用户答了用途 ⇒ 下一轮追问文案里必须出现「用途通勤」
   （这一条才真的钉住「答了要算数」）；
2. **不再问**：已答的用途不得**再被问**（两种机制——追问卡与 `followup`——都不许）；
3. **无条件断言**：不用 `if clarification is not None:` 包着——
   否则「追问机制整个坏掉、永远返回 None」也会让测试照绿（**空转**）；
4. **不回归**：用户**没有**回答时，仍要问。

## 与姊妹文件的关系

`test_agent_production_replay_1247.py` 按**时间线**复刻 2026-10-23 的真人对话；
本文件按**机制**盯「用户答了某一项」⇒ 两者互补，不重复。
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


def _question_of(payload: dict) -> str:
    """这一轮的追问文案（前端渲染的就是它，见 AgentChat.tsx 的 ClarificationChips）。"""
    if payload.get("clarification"):
        return payload["clarification"].get("question") or ""
    if payload.get("followup"):
        return payload["followup"].get("question") or ""
    return ""


def test_answered_usage_is_echoed_back(db_session: Session, client: TestClient):
    """**核心判据**：用户答了用途 ⇒ 下一轮的追问文案**必须回显**它。

    这是「答了要算数」的**可观测**形态：回执由 `_priority_phrase` 读
    `profile.usage` 生成 ⇒ 用途没进画像 ⇒ 回执里就没有「用途」。

    ⚠️ 判据**不能**写成「不许再问用途」——实测把用途整个解析不出来时，
    引擎压根不会走到「问用途」那一步，「不许再问」照样成立 ⇒ 判据不区分 ⇒ 假绿。
    """
    _seed(db_session)
    session_id = _create_session(client)

    # 第 0 轮：只给预算（**不**说「有什么能选」）⇒ 引擎发**追问卡**问用途
    first = _send(client, session_id, "预算12万油车")
    assert first.get("need_clarification"), f"第 0 轮应发追问卡：{first}"
    assert first["clarification"].get("missing") == ["usage"], first

    # 第 1 轮：用户**明确回答用途**
    second = _send(client, session_id, "上下班通勤")

    # ✅ 用途必须被回显（这才证明「答了要算数」）
    question = _question_of(second)
    assert "通勤" in question, f"用户答了用途，追问文案却没回显：{question!r} / {second}"


def test_answered_usage_is_not_asked_again(db_session: Session, client: TestClient):
    """已答的用途**不得再被问**（追问卡与 `followup` 两种机制都不许）。"""
    _seed(db_session)
    session_id = _create_session(client)

    _send(client, session_id, "预算12万油车")
    second = _send(client, session_id, "上下班通勤")

    assert "usage" not in _asked_missing(second), f"用户已答用途，却又被问用途：{second}"


def test_answering_pending_question_still_allows_asking_others(
    db_session: Session, client: TestClient
):
    """答了用途后**仍可问人数**——但不能因此「装作用途没被回答」。

    真人销售会问细节，但不会假装没听见上一句。
    """
    _seed(db_session)
    session_id = _create_session(client)

    _send(client, session_id, "预算12万油车")
    second = _send(client, session_id, "上下班通勤")

    missing = _asked_missing(second)
    assert missing != ["usage"], f"不该再问用途：{second}"
    if missing:
        # 若还在追问，**追问文案本身**必须体现已收到的用途
        question = _question_of(second)
        assert question.strip(), f"追问文案不能为空：{second}"
        assert "通勤" in question, f"追问文案应体现已收到的用途（已记住…通勤）：{second}"


def test_unanswered_question_still_asks(db_session: Session, client: TestClient):
    """**不回归**：用户**没有**回答被追问的那一项 ⇒ 仍要问。"""
    _seed(db_session)
    session_id = _create_session(client)

    first = _send(client, session_id, "预算12万油车")
    assert "usage" in _asked_missing(first), f"没回答时应问用途：{first}"


def test_next_clarification_respects_answered_usage():
    """单元层：`next_clarification` 在画像**已含**用途时不得再问用途。

    这是上面端到端用例的判据内核——若这一层问用途，端到端必然问。

    ⚠️ 断言写成**无条件**：初版用 `if clarification is not None:` 包着，
    于是「追问机制整个坏掉、永远返回 None」也会让测试照绿（**空转**）。
    改成无条件后：既要「不再问用途」，也要「确实还在追问（问人数）」。
    """
    profile = UserProfile()
    profile.budget = Budget(max=120000.0, currency="CNY", type="official_msrp")
    profile.usage = ["通勤"]
    from app.agent.engine import next_clarification

    clarification = next_clarification(profile)
    assert clarification is not None, "用途已答、人数未答 ⇒ 应当继续追问人数"
    assert clarification.missing == ["passengers"], clarification
