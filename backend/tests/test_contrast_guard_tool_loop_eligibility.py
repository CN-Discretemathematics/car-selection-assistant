"""0.65 对比措辞守卫与 0.75 工具循环的**共用判定**（2026-10-05 实测缺陷回归）。

缺陷本身：守卫 `not (_CONTRAST_ASK_RE.search(m) and len(resolved) >= 2)` 的语义是
「对比问题让给工具循环」，它**假定** 0.75 一定接得住；但 0.75 还要求
`not profile_core`，守卫没算这一条。于是真实多轮会话里最常见的时序

    第1轮「预算20万，汉怎么样」  → 锁定汉，画像已有 budget
    第2轮「对比一下汉L」        → 并入后 resolved=2，守卫让出车系问答
                                 → 0.75 被 profile_core 拦下
                                 → 掉到 4:recommendation_fallback → 答「预算大概多少？」

**用户第 1 轮就把预算说过了，第 2 轮反而被再问一次。**

本文件同时钉住两件事，缺一不可：
  ① 修复方向正确（该留车系问答应留）；
  ② **没有过度修复**——0.75 原本的目标场景（无核心约束）必须仍然走工具循环。
只钉 ① 的话，把守卫整个删掉也能让 ① 通过。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.routing import (
    _CONTRAST_ASK_RE,
    _tool_loop_eligible,
    decide_route,
)
from app.agent.schemas import Budget, UserProfile
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

#: 含字面「对比」的措辞（`_CONTRAST_ASK_RE` 只认这两个字）
CONTRAST_MSGS = ("对比一下汉L", "汉L和汉对比", "对比下汉跟汉L")
#: 不含「对比」二字的对比说法——守卫本来就管不着它们，列在这里做对照基线
OTHER_COMPARE_MSGS = ("汉L和汉哪个好", "和汉L比呢", "汉L跟汉比一下")


def _seed_pair(db: Session):
    src = make_source(db, name="汽车之家")
    brand = make_brand(db, name="比亚迪", source=src)
    han = make_series(db, brand, name="汉", energy_types=("BEV",), source=src)
    hanl = make_series(db, brand, name="汉L", energy_types=("BEV",), source=src)
    for s in (han, hanl):
        year = make_year(db, s)
        make_variant(
            db, s, year, config_version="旗舰", energy_type="BEV",
            price_cny="250000", source=src,
        )
    db.commit()
    return han, hanl, brand


def _profile(han, *, budget: bool) -> UserProfile:
    """第 1 轮留下的画像：锁定了汉；budget 决定第 2 轮会不会被反问预算。"""
    p = UserProfile()
    if budget:
        p.budget = Budget(min=180000, max=280000)
    p.locked_series_ids = [han.id]
    return p


@pytest.mark.parametrize("msg", CONTRAST_MSGS)
def test_contrast_with_known_budget_stays_in_series_qa(db_session: Session, msg: str):
    """缺陷本身：说过了预算，第 2 轮问对比不该被反问预算。"""
    han, hanl, brand = _seed_pair(db_session)
    decision = decide_route(
        msg, {}, True, _profile(han, budget=True),
        [(han, brand), (hanl, brand)], db_session,
    )
    assert decision.intent == "series_qa", (
        f"含「对比」+ 已锁车系 + 画像已有预算时落到了 {decision.intent}"
        f"（{decision.matched_rule}），用户会被反问已经说过的预算"
    )


@pytest.mark.parametrize("msg", CONTRAST_MSGS)
def test_contrast_without_core_constraints_still_goes_tool_loop(
    db_session: Session, msg: str
):
    """**不得过度修复**：0.75 原本的目标场景（无核心约束）必须仍然走工具循环。

    只钉「该留的留下」的话，把守卫整段删掉同样能让上一条通过——这条是防线。
    """
    han, hanl, brand = _seed_pair(db_session)
    decision = decide_route(
        msg, {}, True, _profile(han, budget=False),
        [(han, brand), (hanl, brand)], db_session,
    )
    assert decision.intent == "tool_loop", (
        f"无核心约束时「{msg}」本就该落工具循环，实际 {decision.intent}"
    )


@pytest.mark.parametrize("msg", CONTRAST_MSGS + OTHER_COMPARE_MSGS)
@pytest.mark.parametrize("budget", [True, False])
def test_contrast_never_falls_into_recommendation(
    db_session: Session, msg: str, budget: bool
):
    """不变式：点名了两台车要对比，就绝不该掉进推荐兜底。

    `recommendation` 在这里的含义是「信息不够，去问用户」——而用户**已经点名了两台车**，
    事实全在库里。这是本缺陷最本质的错：不是答得不好，是**答非所问**。
    """
    han, hanl, brand = _seed_pair(db_session)
    decision = decide_route(
        msg, {}, True, _profile(han, budget=budget),
        [(han, brand), (hanl, brand)], db_session,
    )
    # 无条件断言：两种预算 × 六种对比措辞，**一律**不得掉进推荐兜底。
    # （早先这里写成 `if len(_CONTRAST_ASK_RE.findall(msg)) >= 1:`，
    #  于是 3 条对照措辞被静默跳过——参数化了却只校验一半，等于半个永真断言。）
    assert decision.intent != "recommendation", (
        f"点名两台车要对比却落到了 {decision.intent}（budget={budget}）："
        "recommendation 的含义是「信息不够去问用户」，而事实全在库里"
    )


def test_tool_loop_eligible_is_false_when_profile_has_budget(db_session: Session):
    """共用判定本身的语义：画像有核心约束 → 0.75 不可达。

    守卫问的就是这句话。原先守卫没问，于是无条件让出。
    """
    han, hanl, brand = _seed_pair(db_session)
    resolved = [(han, brand), (hanl, brand)]
    assert not _tool_loop_eligible(
        "对比一下汉L", {}, _profile(han, budget=True), resolved, db_session
    )
    assert _tool_loop_eligible(
        "对比一下汉L", {}, _profile(han, budget=False), resolved, db_session
    )


def test_guard_diverts_only_when_tool_loop_would_take(db_session: Session):
    """守卫让出的**充要条件**就是「0.75 接得住」——两者由同一函数判定。

    这条把「让出」和「接住」绑在一起：只要有人再往守卫里手写一套独立条件，
    这个等价关系就会破，而本文件其余用例仍会绿——所以它必须单独钉住。
    """
    han, hanl, brand = _seed_pair(db_session)
    resolved = [(han, brand), (hanl, brand)]
    for msg in CONTRAST_MSGS + OTHER_COMPARE_MSGS:
        for budget in (True, False):
            profile = _profile(han, budget=budget)
            diverts = (
                bool(_CONTRAST_ASK_RE.search(msg))
                and len(resolved) >= 2
                and _tool_loop_eligible(msg, {}, profile, resolved, db_session)
            )
            if diverts:
                continue
            # len(resolved)>=2 ⇒ should_answer 恒真，故「不让出」必须等于「走车系问答」
            decision = decide_route(msg, {}, True, profile, resolved, db_session)
            assert decision.intent == "series_qa", (
                f"守卫让出后落到了 {decision.intent}，"
                f"但 0.75 并不会接住（msg={msg}, budget={budget}）"
            )


def _seed_han_scene(db_session: Session) -> dict:
    """汉 / 汉L 两车系，各带可对比的参数事实（端到端用）。"""
    src = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="比亚迪", source=src)
    han = make_series(db_session, brand, name="汉", body_type="sedan",
                      energy_types=("BEV",), source=src)
    hanl = make_series(db_session, brand, name="汉L", body_type="sedan",
                       energy_types=("BEV",), source=src)
    han.positioning = "中大型轿车"
    hanl.positioning = "中大型轿车"
    for series, price, rng, batt in ((han, "220000", "605", "76.9"),
                                     (hanl, "280000", "701", "100.0")):
        year = make_year(db_session, series)
        make_variant(
            db_session, series, year, config_version="旗舰版",
            energy_type="BEV", price_cny=price, source=src,
            facts=[
                ("参数信息", "CLTC纯电续航里程(km)", rng, "km", "CLTC"),
                ("参数信息", "电池能量(kWh)", batt, "kWh", None),
            ],
        )
    db_session.commit()
    return {"han": han.id, "hanl": hanl.id}


def test_end_to_end_turn2_compares_instead_of_reasking_budget(
    client: TestClient, db_session: Session
):
    """④ 的端到端断言（原先因「合并后仍不进对比链路」而撤下，本次补回）。

    真实多轮时序：
        第1轮「预算20万，汉怎么样」 → 锁定汉
        第2轮「对比一下汉L」       → 本轮只解析出汉L，另一台在**会话历史**里

    修之前：落 `4:recommendation_fallback` → 答「预算大概多少？」，
          而用户第 1 轮就说过了预算；两台车的参数全在库里，一次都没被比较。
    修之后：落 `series_qa` → 直接给两车对比。

    只断言 intent 不够——intent 对了也可能渲染出空答案，所以断言**正文里两台车都在**。
    """
    ids = _seed_han_scene(db_session)
    assert ids["han"] != ids["hanl"], "两台车必须是不同车系，否则下面的断言无意义"
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "预算20万，汉怎么样"},
    )
    turn2 = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "对比一下汉L"},
    ).json()

    explanation = turn2.get("explanation") or ""
    assert "预算大概多少" not in explanation, (
        f"用户第 1 轮已给过预算，第 2 轮不该被反问。实际回复：{explanation[:200]}"
    )
    assert turn2.get("need_clarification") is not True, (
        f"不该在这一轮转入追问。实际 need_clarification={turn2.get('need_clarification')}"
    )
    assert "汉" in explanation, f"对比答案里应有锁定的那台车。实际：{explanation[:200]}"
    assert "汉L" in explanation, f"对比答案里应有本轮点名的车。实际：{explanation[:200]}"
    # 车名出现只能证明「提到了」，不能证明「比了」——断言**两台车各自的参数值都出现**
    # （汉 605km / 汉L 701km）。只回答一台车也含「汉」和「汉L」字样，这条才拦得住。
    assert "605" in explanation and "701" in explanation, (
        "两台车的续航值都应出现在对比里（汉 605 / 汉L 701）。"
        f"实际：{explanation[:300]}"
    )
