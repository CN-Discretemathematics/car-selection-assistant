"""上下文追问（「还有别的吗」）应落到**再推一批**，而不是普通对话。

## 缺陷（2026-10-21 实测，本地快照库）

多轮题库的第三轮（追问）里，「还有别的吗」「再看看其他的」**11/11 没有结构化推荐**
——引擎回的是一句闲聊模板「可以告诉我你的预算、主要用途和乘坐人数吗？」，
**而此时画像里预算/用途/人数俱全**（探针逐轮打印确认）。
用户明确在要车，引擎却反问它已经知道的 ⇒ 答非所问。

根因：路由规则 1「无购车意图 → 普通对话」只看**本句**有没有购车关键词，
而这些追问句**本句就没有**（"还有别的吗"里没有「推荐」「预算」「车」）。

## 修法

`decide_route` 规则 1a：句子匹配**追问句式**（`asks_more_cars`）**且**
会话画像已有**核心约束**（预算/用途/人数/车身）⇒ 落 `recommendation`。

守卫的作用：没有购车上下文的「还有别的吗」（如「还有别的优惠吗」）仍走普通对话。
"""
from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from app.agent.engine import AgentEngine
from app.agent.routing import asks_more_cars, decide_route
from app.agent.schemas import UserProfile
from app.agent.session import SessionStore
from app.common.llm import LLMClient
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db_session: Session) -> None:
    """种一台符合档位的车：比亚迪 · 轿车 · 纯电 · 15 万。"""
    source = make_source(db_session, name="追问测试来源")
    brand = make_brand(db_session, name="比亚迪", source=source)
    series = make_series(
        db_session, brand, name="测试车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV",
        price_cny="150000", source=source,
    )
    db_session.commit()


def _seed_many(db_session: Session, count: int = 3) -> None:
    """种 count 个**不同车系**（同品牌同档），每个车系**两个款型**。

    两个款型是必要的：按**款型**排除（而不是按车系）的实现会漏掉同车系的另一个款型 ⇒
    用户看到的还是那个车系。款型只有一个时测不出这个差别。
    """
    source = make_source(db_session, name="换批测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    for i in range(count):
        series = make_series(
            db_session, brand, name=f"换批车系{i}", body_type="sedan",
            energy_types=("BEV",), source=source,
        )
        year = make_year(db_session, series)
        for cfg in ("标准版", "尊贵版"):
            make_variant(
                db_session, series, year, config_version=cfg, energy_type="BEV",
                price_cny="150000", source=source,
            )
    db_session.commit()


# ── 句式：只收「再给一批」的追问 ────────────────────────────────────────────


def test_asks_more_cars_matches_the_reask_phrases():
    for msg in (
        "还有别的吗",
        "还有吗",
        "还有别的车吗",
        "还有推荐吗",
        "还有其他的吗",
        "再看看其他的",
        "再看看别的",
        "再来几款",
        "再来一批",
        "就这些吗",
        "其他的呢",
        "还有别的吗？",
    ):
        assert asks_more_cars(msg), msg


def test_asks_more_cars_rejects_other_sentences():
    """这些**不是**要车，不能被追问句式吃掉。"""
    for msg in (
        "还有问题吗",
        "还有别的优惠吗",
        "还有别的手机推荐吗",
        "还有别的条件吗",
        "今天天气不错",
        "帮我算个题",
        "预算20万",
        "预算20万，通勤，两三个人",
    ):
        assert not asks_more_cars(msg), msg


# ── 路由：有没有购车上下文，决定落哪条链路 ────────────────────────────────


def _profile_with_budget() -> UserProfile:
    return UserProfile(
        budget={"min": 120000.0, "max": 180000.0, "currency": "CNY", "type": "official_msrp"},
        usage=["通勤"],
        passengers=3,
        body_type=["sedan"],
    )


def test_reask_with_car_context_routes_to_recommendation(db_session: Session):
    """会话里已有明确购车诉求 ⇒ 「还有别的吗」⇒ 再推一批。"""
    decision = decide_route(
        "还有别的吗", {}, False, _profile_with_budget(), [], db_session
    )
    assert decision.intent == "recommendation", decision
    assert decision.matched_rule.startswith("1a:"), decision
    assert decision.signals.get("reask") is True, decision


def test_reask_without_car_context_stays_chitchat(db_session: Session):
    """没有购车上下文 ⇒ 「还有别的吗」仍走普通对话（不得凭空开始推荐）。"""
    decision = decide_route("还有别的吗", {}, False, UserProfile(), [], db_session)
    assert decision.intent == "chitchat", decision


def test_reask_in_a_non_car_topic_stays_chitchat(db_session: Session):
    """非车话题里的「还有别的吗」不得被追问句式带进推荐。

    守卫有两道：句式**整句**匹配（「还有别的手机吗」不匹配），
    以及会话画像里必须已有**购车**核心约束。
    """
    profile = _profile_with_budget()
    decision = decide_route("还有别的手机吗", {}, False, profile, [], db_session)
    assert decision.intent == "chitchat", decision


def test_normal_car_question_still_routes_to_recommendation(db_session: Session):
    """回归：带购车关键词的句子照旧走推荐（本次改动不得影响既有链路）。"""
    decision = decide_route(
        "预算20万，通勤，两三个人", {"budget": {"min": 200000}}, True,
        UserProfile(), [], db_session,
    )
    assert decision.intent == "recommendation", decision


# ── 端到端：追问那一轮必须真的给出结构化推荐 ──────────────────────────────


def test_reask_turn_produces_structured_recommendations(db_session: Session):
    """第一轮给需求，第二轮「还有别的吗」⇒ **有卡片**，不是一句闲聊。

    ⚠️ 要种**超过一轮展示量**的车：池子被推完后「再给一批」没有可给的了
    （那时给的是「都推过了」的诚实文案，见下面的用例）⇒ 用例会假红。
    """
    _seed_many(db_session, 7)
    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())
    sid = engine._store.create()

    first = asyncio.run(
        engine.respond(db_session, sid, "预算15万，通勤，两三个人用")
    )
    assert first.recommended_variants, "第一轮就该有推荐（前提）"

    second = asyncio.run(engine.respond(db_session, sid, "还有别的吗"))
    assert second.recommended_variants, (
        "「还有别的吗」必须再推一批，而不是回一句闲聊"
        f"（实得：{second.explanation!r}）"
    )


# ── 「再给一批」：给**没给过**的车，而不是逐位相同的那一批 ──────────────────
#
# 引擎是确定性的 ⇒ 同一画像必然推回同一批（实测：宽需求下候选池 3162 台，
# 第二轮仍是同一批 5 台）。用户要「更多」，必须把**已经给过的车系**排除掉。


def _series_names(out) -> set[str]:
    return {v.series_name for v in out.recommended_variants or []}


def test_reask_gives_a_batch_of_unseen_series(db_session: Session):
    """「再推荐几款」⇒ 第二轮的车系与第一轮**零重叠**。"""
    _seed_many(db_session, 7)
    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())
    sid = engine._store.create()

    first = asyncio.run(engine.respond(db_session, sid, "预算15万，通勤，两三个人用"))
    assert first.recommended_variants, "第一轮就该有推荐（前提）"
    second = asyncio.run(engine.respond(db_session, sid, "再推荐几款"))
    assert second.recommended_variants, "第二轮该给下一批"
    assert not (_series_names(first) & _series_names(second)), (
        f"第二轮把第一轮的车又推了一遍：{_series_names(first)} vs {_series_names(second)}"
    )


def test_restating_the_requirement_returns_the_original_batch(db_session: Session):
    """**不是**追问的那一轮（重新说需求）⇒ 回到原来那一批。

    排除只在**追问轮**生效——用户重新问需求时看到不同的车会以为是系统不稳定。
    """
    _seed_many(db_session, 3)
    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())
    sid = engine._store.create()

    first = asyncio.run(engine.respond(db_session, sid, "预算15万，通勤，两三个人用"))
    again = asyncio.run(engine.respond(db_session, sid, "预算15万，通勤，两三个人用"))
    assert _series_names(first) == _series_names(again), (
        "同样的需求必须给出同样的一批（确定性）"
    )


def test_reask_when_pool_is_exhausted_says_already_shown(db_session: Session):
    """池子里的车都推过了 ⇒ 如实说「都推过了」，**不是**「没有符合条件的车」。

    后者会让用户以为自己的预算/用途有问题——而事实是条件都匹配、只是都给过了。
    """
    _seed(db_session)  # 只有一台 ⇒ 推完就没了
    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())
    sid = engine._store.create()

    first = asyncio.run(engine.respond(db_session, sid, "预算15万，通勤，两三个人用"))
    assert first.recommended_variants, "第一轮就该有推荐（前提）"
    second = asyncio.run(engine.respond(db_session, sid, "再推荐几款"))
    assert not second.recommended_variants, "池子空了，不该再给车"
    text = " ".join(second.reasons or []) + (second.explanation or "")
    assert "推过了" in text, text


def test_tool_excludes_the_given_series(db_session: Session):
    """工具层：传入 `exclude_series_ids` ⇒ 那个车系的**全部款型**都不进候选。

    顺带钉住**车系级**语义：同一车系的另一个款型也必须被排掉
    （按款型排除的实现会漏掉它 ⇒ 用户看到的还是那个车系）。
    """
    from app.agent.tools import recommendation_tool

    source = make_source(db_session, name="工具排除测试来源")
    brand = make_brand(db_session, name="排除测试品牌", source=source)
    for i in range(2):
        series = make_series(
            db_session, brand, name=f"排除车系{i}", body_type="sedan",
            energy_types=("BEV",), source=source,
        )
        year = make_year(db_session, series)
        # 同一车系两个款型：排除车系 ⇒ 两个款型都该走
        for cfg in ("标准版", "尊贵版"):
            make_variant(
                db_session, series, year, config_version=cfg, energy_type="BEV",
                price_cny="150000", source=source,
            )
    db_session.commit()

    profile = UserProfile(
        budget={"min": 100000.0, "max": 200000.0, "currency": "CNY", "type": "official_msrp"},
        usage=["通勤"],
        passengers=3,
        body_type=["sedan"],
    )
    plain = recommendation_tool(db_session, profile)
    assert plain["variants"], "前提：不排除时应有候选"
    first_series = plain["variants"][0]["series_id"]

    excluded = recommendation_tool(db_session, profile, exclude_series_ids=[first_series])
    assert all(v["series_id"] != first_series for v in excluded["variants"]), excluded
    # 池子大小（排除**前**）不变——执行层要靠它区分「没匹配到」与「都推过了」
    assert excluded["candidates_scanned"] == plain["candidates_scanned"], excluded
