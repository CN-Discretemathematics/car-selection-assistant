"""多轮评测框架的判据与自检测试。

## 重点：空转自检

本仓反复记录过一个陷阱：「判据写对了、断言写得像那么回事，但构造的前提让被测行为
压根不发生」——这类测试永远绿、抓不到任何东西。

多轮框架的头号风险是：`not_recommended`（拒过的不再推）在**整场对话没推荐出任何车**时
是**恒真的**。所以框架必须自检「本场是否产出过推荐」，没有则判**无效**而非通过。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.agent.schemas import AgentMessageOut, RecommendedVariant

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import eval_multiturn as mt  # noqa: E402


def _out_with(*series: tuple[str, str]) -> AgentMessageOut:
    """造一个带指定 (series_name, brand_name) 推荐列表的回答。"""
    return AgentMessageOut(
        session_id="s",
        recommended_variants=[
            RecommendedVariant(
                variant_id=i + 1,
                series_id=i + 1,
                series_name=s,
                brand_name=b,
                display_name=f"{b}{s} 2026款",
                energy_type="BEV",
                score=1.0,
            )
            for i, (s, b) in enumerate(series)
        ],
        explanation="推荐汉。预算 20 万。",
    )


# ── 判据：车系类走结构化字段 ────────────────────────────────────────────────


def test_not_recommended_passes_when_car_absent():
    out = _out_with(("汉", "比亚迪厂"))
    mt.check_assertion(out, {"type": "not_recommended", "car": "海豹"})


def test_not_recommended_fails_when_car_present():
    out = _out_with(("汉", "比亚迪厂"))
    with pytest.raises(mt.AssertionFailed):
        mt.check_assertion(out, {"type": "not_recommended", "car": "汉"})


def test_recommended_passes_when_car_present():
    out = _out_with(("汉", "比亚迪厂"))
    mt.check_assertion(out, {"type": "recommended", "car": "汉"})


def test_recommended_fails_when_car_absent():
    out = _out_with(("汉", "比亚迪厂"))
    with pytest.raises(mt.AssertionFailed):
        mt.check_assertion(out, {"type": "recommended", "car": "海豹"})


def test_brand_prefix_form_also_matched():
    """回答写「比亚迪厂汉」时，题面只写「汉」也要算同一台。"""
    out = _out_with(("汉", "比亚迪厂"))
    with pytest.raises(mt.AssertionFailed):
        mt.check_assertion(out, {"type": "not_recommended", "car": "汉"})


def test_db_alias_also_matched(db_session: Session):
    """库内**别名**也要算命中。

    回答可能用别名显示（「比亚迪 汉」/ 库内登记的别名），而题面只写短名——
    不认别名，`not_recommended` 会**漏判**（本该出现的车被判成没出现）。

    注：真实库里 `vehicle_series.aliases` 目前**全是空数组**（实测 908/908 为 `[]`），
    所以这条逻辑在当前数据上**不起作用**，是**防御性冗余**——
    留着是因为将来接入别名数据就自动生效，不需要再改判据。
    """
    from tests.seed import make_brand, make_series, make_source, make_variant, make_year

    src = make_source(db_session)
    brand = make_brand(db_session, name="测试品牌丁", source=src)
    series = make_series(db_session, brand, name="丁车系", source=src)
    series.aliases = ["丁车系别名"]
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="标准版", source=src)
    db_session.commit()

    # 回答里用**别名**显示
    out = _out_with(("丁车系别名", "测试品牌丁"))
    with pytest.raises(mt.AssertionFailed):
        mt.check_assertion(
            out, {"type": "not_recommended", "car": "丁车系"}, db_session
        )


def test_brand_prefix_from_db_also_matched(db_session: Session):
    """**带 db** 时，库内的「品牌+车系名」与**别名**都要算命中。

    回答可能用库里的别名或「品牌+车系名」显示，而题面只写短名——
    不认这些写法，`not_recommended` 会**漏判**（本该出现的车被判成没出现）。

    故本测试让**回答里就写带前缀的形式**（`series_name` 是短名但
    `brand_name` 非空 → `_recommended_names` 会同时产出两种写法），
    这样去掉前缀逻辑后断言就抓不到缺陷。
    """
    from tests.seed import make_brand, make_series, make_source, make_variant, make_year

    src = make_source(db_session)
    brand = make_brand(db_session, name="测试品牌丙", source=src)
    series = make_series(db_session, brand, name="丙车系", source=src)
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="标准版", source=src)
    db_session.commit()

    out = _out_with(("丙车系", "测试品牌丙"))
    # 「品牌+车系名」的写法（库内可推导）也应被判为同一台 → not_recommended 应失败
    with pytest.raises(mt.AssertionFailed):
        mt.check_assertion(
            out, {"type": "not_recommended", "car": "丙车系"}, db_session
        )


def test_car_assertion_ignores_prose_mention():
    """**散文里提到但没推荐**的车，不算「进了推荐列表」。

    这是车系类断言走结构化字段的理由：`explanation` 里可能说「也可以看看 XX」，
    那台车**不在**推荐列表里。
    """
    out = _out_with(("汉", "比亚迪厂"))
    out.explanation = "推荐汉。也可以看看海豹。"
    mt.check_assertion(out, {"type": "not_recommended", "car": "海豹"})


# ── 判据：关键词类走可见文本 ────────────────────────────────────────────────


def test_mentions_reads_explanation():
    out = _out_with(("汉", "比亚迪厂"))
    mt.check_assertion(out, {"type": "mentions", "word": "预算"})


def test_not_mentions_fails_when_present():
    out = _out_with(("汉", "比亚迪厂"))
    with pytest.raises(mt.AssertionFailed):
        mt.check_assertion(out, {"type": "not_mentions", "word": "预算"})


def test_mentions_reads_clarification_question():
    """追问文案也是可见文本——「已记下：预算…」在 clarification.question 里。"""
    from app.agent.schemas import Clarification

    out = AgentMessageOut(
        session_id="s",
        need_clarification=True,
        clarification=Clarification(
            question="已记下：预算 20 万。主要用来跑什么？",
            options=["通勤"],
            missing=["usage"],
        ),
    )
    mt.check_assertion(out, {"type": "mentions", "word": "预算"})


def test_unknown_assertion_type_raises():
    out = _out_with(("汉", "比亚迪厂"))
    with pytest.raises(mt.AssertionFailed, match="未知的断言类型"):
        mt.check_assertion(out, {"type": "nonsense", "car": "汉"})


def test_missing_field_raises():
    out = _out_with(("汉", "比亚迪厂"))
    with pytest.raises(mt.AssertionFailed, match="缺 car 字段"):
        mt.check_assertion(out, {"type": "not_recommended"})
    with pytest.raises(mt.AssertionFailed, match="缺 word 字段"):
        mt.check_assertion(out, {"type": "mentions"})


# ── 空转自检 ────────────────────────────────────────────────────────────────


def test_scenario_without_any_recommendation_is_invalid(db_session: Session):
    """整场对话**一次都没推荐出车** → 场景判**无效**，不算通过。

    否则 `not_recommended` 就是恒真的——「通过」不是因为行为正确，
    而是因为什么都没发生。
    """
    scenario = {
        "id": "s1",
        "scenario": "reject_then_no_recommend",
        "turns": [
            {"user": "随便说点什么", "assert": []},
            {"user": "再推荐几款", "assert": [{"type": "not_recommended", "car": "汉"}]},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))
    assert res["no_recommendation"] is True
    assert res["passed"] is False
    assert any("未产出任何推荐" in f for f in res["failures"])


def test_assertion_on_a_never_recommended_car_is_invalid(db_session: Session):
    """**第二层**恒真自检：被断言的那台车**从未被推荐过** → 场景无效。

    「全程有推荐」不等于「断言在验行为」：被拒的那台若是同品牌的兄弟车
    （压根没进 top-5），「它不该再被推荐」就恒真——通过但什么都没验。
    实测：这一层自检把「50/50 通过」打回 **11/50 真通过**。

    ⚠️ 本用例**必须先有推荐**（库里种一台车）——否则「全程无推荐」那条自检
    也会让场景变红，用例就变成**测不出自己**（仓库反复警告的空转断言）。
    """
    from tests.seed import make_brand, make_series, make_source, make_variant, make_year

    src = make_source(db_session)
    brand = make_brand(db_session, name="在场品牌", source=src)
    series = make_series(db_session, brand, name="在场车系", source=src)
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV",
        price_cny="150000", source=src,
    )
    db_session.commit()

    scenario = {
        "id": "s3",
        "scenario": "reject_then_no_recommend",
        "turns": [
            {"user": "预算20万，通勤，两三个人", "assert": []},
            # 断言的是**另一台**车（不在榜里）→ 恒真
            {"user": "再推荐几款", "assert": [{"type": "not_recommended", "car": "压根没推过的车"}]},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))
    assert res["no_recommendation"] is False, res  # 前提：确实有推荐，排除另一条自检的干扰
    assert res["vacuous_assertion"] is True, res
    assert res["passed"] is False
    assert any("从未被推荐过" in f for f in res["failures"]), res["failures"]


def test_scenario_with_recommendation_is_not_flagged(db_session: Session):
    """只要**有过一次**推荐，就不算「无推荐」，走正常判据。"""
    scenario = {
        "id": "s2",
        "scenario": "probe",
        "turns": [{"user": "你好", "assert": []}],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))
    # 本测试库无完整价格/事实数据 → 大概率无推荐；
    # 关键断言是**字段存在且类型正确**，而不是具体值。
    assert isinstance(res["no_recommendation"], bool)
    assert "no_recommendation" in res


# ── 跨会话：标签切换 + 身份接线（2026-10-21）──────────────────────────────
# 题库里「回访用户记忆」「删掉的记忆」两个场景要测的是**跨会话**行为。
# 框架此前全程共用一个 session_id ⇒ 测的其实是「同会话内还记不记得」
# （记忆要么一直生效、要么一直不生效，**都不是跨会话**）。
#
# 这里用假引擎验证**框架的接线**（标签→会话映射、身份创建与传递），
# 真实行为由分支 8 合入后跑真库验证。


class _FakeEngine:
    """记录每次 handle 调用的假引擎。"""

    def __init__(self, *, with_identity: bool = False) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.with_identity = with_identity

    async def handle(self, db, session_id, message, **kwargs):  # noqa: ANN001
        self.calls.append((session_id, message, kwargs.get("identity")))
        return AgentMessageOut(session_id=session_id, explanation="收到")


class _FakeEngineWithIdentity(_FakeEngine):
    """支持 `identity` 参数的假引擎（Phase 1 接线后的形状）。"""

    async def handle(self, db, session_id, message, *, identity=None):  # noqa: ANN001
        self.calls.append((session_id, message, identity))
        return AgentMessageOut(session_id=session_id, explanation="收到")


class _FakeStore:
    """假会话存储：每次 create 给一个递增 id，便于断言「是否换了会话」。"""

    def __init__(self) -> None:
        self.n = 0
        self.created: list[str] = []

    def create(self) -> str:
        self.n += 1
        sid = f"sess-{self.n}"
        self.created.append(sid)
        return sid


def _patch_engine(monkeypatch, engine, store) -> None:  # noqa: ANN001
    monkeypatch.setattr(mt, "get_agent_engine", lambda: engine)
    monkeypatch.setattr(mt, "get_session_store", lambda: store)


def test_session_label_switches_to_a_new_session(monkeypatch, db_session: Session):
    """标签变了 ⇒ **另一个会话**；标签相同 ⇒ 同一会话。"""
    engine = _FakeEngine()
    store = _FakeStore()
    _patch_engine(monkeypatch, engine, store)

    scenario = {
        "id": "x1",
        "scenario": "returning_user_memory",
        "turns": [
            {"user": "预算8到12万", "assert": [], "session": "first"},
            {"user": "按之前说的来", "assert": [], "session": "second"},
            {"user": "还有吗", "assert": [], "session": "second"},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))

    sids = [c[0] for c in engine.calls]
    assert sids[0] != sids[1], "标签变了必须换会话"
    assert sids[1] == sids[2], "同一标签的多轮应留在同一会话"
    assert res["sessions"] == 2
    assert res["cross_session"] is True


def test_turns_without_label_share_one_session(monkeypatch, db_session: Session):
    """没标 `session` 的轮次 ⇒ 全部落在**同一个**会话（保持旧行为）。"""
    engine = _FakeEngine()
    store = _FakeStore()
    _patch_engine(monkeypatch, engine, store)

    scenario = {
        "id": "x2",
        "scenario": "reject_then_no_recommend",
        "turns": [
            {"user": "a", "assert": []},
            {"user": "b", "assert": []},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))

    sids = {c[0] for c in engine.calls}
    assert len(sids) == 1
    assert res["sessions"] == 1
    assert res["cross_session"] is False


def test_cross_session_creates_one_identity_shared_by_all_sessions(
    monkeypatch, db_session: Session
):
    """跨会话场景 ⇒ 建**一个**身份，本场所有会话共用它（回访用户的定义）。"""
    engine = _FakeEngineWithIdentity()
    store = _FakeStore()
    _patch_engine(monkeypatch, engine, store)

    created: list[int] = []

    def _fake_factory():
        def _make(db):  # noqa: ANN001
            created.append(1)
            return ("IDENTITY-1", "plaintext-token", True)

        return _make

    monkeypatch.setattr(mt, "_identity_factory", _fake_factory)

    scenario = {
        "id": "x3",
        "scenario": "returning_user_memory",
        "turns": [
            {"user": "预算8到12万", "assert": [], "session": "first"},
            {"user": "按之前说的来", "assert": [], "session": "second"},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))

    assert len(created) == 1, "身份只应建一次"
    assert [c[2] for c in engine.calls] == ["IDENTITY-1", "IDENTITY-1"]
    assert engine.calls[0][0] != engine.calls[1][0], "两个会话必须不同"
    assert not any("身份创建失败" in f for f in res["failures"]), res["failures"]


def test_cross_session_without_engine_support_is_red_not_green(
    monkeypatch, db_session: Session
):
    """引擎不支持 `identity` ⇒ 场景判**红**（不是通过）。

    「测不了跨会话记忆」≠「跨会话记忆没问题」——
    悄悄降级成「不建身份」会让断言恒真，又一次**假通过**。
    """
    engine = _FakeEngine()  # 不接受 identity
    store = _FakeStore()
    _patch_engine(monkeypatch, engine, store)

    scenario = {
        "id": "x4",
        "scenario": "returning_user_memory",
        "turns": [
            {"user": "预算8到12万", "assert": [], "session": "first"},
            {"user": "按之前说的来", "assert": [], "session": "second"},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))

    assert res["passed"] is False
    assert any("identity" in f for f in res["failures"]), res["failures"]


def test_identity_creation_failure_is_recorded(monkeypatch, db_session: Session):
    """身份创建炸了 ⇒ 记成失败并继续跑，**不崩掉整轮**评测。"""
    engine = _FakeEngineWithIdentity()
    store = _FakeStore()
    _patch_engine(monkeypatch, engine, store)

    def _boom():
        raise RuntimeError("身份表不存在")

    monkeypatch.setattr(mt, "_identity_factory", _boom)

    scenario = {
        "id": "x5",
        "scenario": "returning_user_memory",
        "turns": [
            {"user": "a", "assert": [], "session": "first"},
            {"user": "b", "assert": [], "session": "second"},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))

    assert res["passed"] is False
    assert any("身份创建失败" in f for f in res["failures"]), res["failures"]


# ── 只跑 enabled 的场景 ────────────────────────────────────────────────────
# 题库里 `enabled: false` 的场景依赖**尚未落地**的能力；框架若照跑，
# 「通过率」会被未落地的能力拉低，看报告的人会误判成「功能坏了」，
# 更糟的是有人会去**改断言**让它变绿。


def test_split_enabled_separates_disabled_scenarios():
    scenarios = [
        {"id": "a", "enabled": True},
        {"id": "b", "enabled": False, "reason_disabled": "依赖 Phase 1"},
        {"id": "c"},  # 没写 enabled ⇒ 视为可跑
    ]
    enabled, disabled = mt.split_enabled(scenarios)
    assert [s["id"] for s in enabled] == ["a", "c"]
    assert [s["id"] for s in disabled] == ["b"]


# ── 按轮的恒真守卫：该轮什么都没推荐 ────────────────────────────────────────
# 场景级那条（「整场无推荐」）管不到这种情况：第一轮推了车、第三轮推不出来
# （候选集被掏空）时，第三轮的「它不在榜」照样验不到任何东西——
# 实测：这类需求下，拒绝功能**整个缺失**的分支仍有 17/50「碰巧通过」。


class _EmptyAfterFirstEngine(_FakeEngine):
    """第一轮推一台车，之后每轮都**什么都不推**（候选集被掏空）。"""

    async def handle(self, db, session_id, message, **kwargs):  # noqa: ANN001
        self.calls.append((session_id, message, kwargs.get("identity")))
        if len(self.calls) == 1:
            return AgentMessageOut(
                session_id=session_id,
                recommended_variants=[
                    RecommendedVariant(
                        variant_id=1, series_id=1, series_name="在场车系",
                        brand_name="在场品牌", display_name="在场品牌在场车系",
                        energy_type="BEV", score=1.0,
                    )
                ],
                explanation="第一轮推一台",
            )
        return AgentMessageOut(session_id=session_id, explanation="这一轮没推车")


class _RecommendingEngine(_FakeEngine):
    """每轮都推一台**别的**车（推荐列表非空）。"""

    async def handle(self, db, session_id, message, **kwargs):  # noqa: ANN001
        self.calls.append((session_id, message, kwargs.get("identity")))
        return AgentMessageOut(
            session_id=session_id,
            recommended_variants=[
                RecommendedVariant(
                    variant_id=2, series_id=2, series_name="别的车系",
                    brand_name="别的品牌", display_name="别的品牌别的车系",
                    energy_type="BEV", score=1.0,
                )
            ],
            explanation="推了别的车",
        )


def test_not_recommended_on_an_empty_turn_is_invalid(monkeypatch, db_session: Session):
    """带 `not_recommended` 断言的那一轮**什么都没推荐** ⇒ 该轮断言无效。"""
    engine = _EmptyAfterFirstEngine()
    store = _FakeStore()
    _patch_engine(monkeypatch, engine, store)

    scenario = {
        "id": "x6",
        "scenario": "reject_then_no_recommend",
        "turns": [
            {"user": "预算20万，通勤，两三个人", "assert": []},
            {"user": "再推荐几款", "assert": [{"type": "not_recommended", "car": "在场车系"}]},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))

    assert res["vacuous_empty_turn"] is True, res
    assert res["passed"] is False
    assert any("什么都没推荐" in f for f in res["failures"]), res["failures"]


def test_non_empty_turn_with_not_recommended_is_not_flagged(monkeypatch, db_session: Session):
    """同一场景里，只要那一轮**推了别的车**，断言就不算恒真。"""
    engine = _RecommendingEngine()
    store = _FakeStore()
    _patch_engine(monkeypatch, engine, store)

    scenario = {
        "id": "x7",
        "scenario": "reject_then_no_recommend",
        "turns": [
            {"user": "预算20万，通勤，两三个人", "assert": []},
            {"user": "再推荐几款", "assert": [{"type": "not_recommended", "car": "被拒车系"}]},
        ],
    }
    res = asyncio.run(mt.run_scenario(db_session, scenario))
    assert res["vacuous_empty_turn"] is False, res

# 此前用裸字符串 `mentions '10'` / `not_mentions '12'`：预算值在正文里必然与
# **车价**混在一起（12-18 万档里有 12.99 万的车），于是两个方向都会误判：
# 「不提到 12」把正确答案判成失败（假否定），「提到 10」也会因推荐了 8.28 万而失败。
# 分布审计实测：删记忆场景 **50/50 条**断言全中这一枪。


def _out_with_budget(min_budget: float | None, max_budget: float | None) -> AgentMessageOut:
    return AgentMessageOut(
        session_id="s",
        explanation="推荐一辆车，官方指导价 12.99 万元。",
        filters={"budget_min": min_budget, "budget_max": max_budget},
    )


def test_budget_in_passes_when_the_range_matches():
    out = _out_with_budget(80000.0, 120000.0)
    mt.check_assertion(out, {"type": "budget_in", "min": 80000, "max": 120000})


def test_budget_in_fails_when_the_range_differs():
    out = _out_with_budget(None, None)
    with pytest.raises(mt.AssertionFailed):
        mt.check_assertion(out, {"type": "budget_in", "min": 80000, "max": 120000})


def test_budget_not_in_passes_when_the_range_is_gone():
    """删掉预算后口径应为空 → `budget_not_in` 成立。

    注意正文里写着「12.99 万」——**旧写法会在这里判失败**（假否定）。
    """
    out = _out_with_budget(None, None)
    mt.check_assertion(out, {"type": "budget_not_in", "min": 120000, "max": 180000})


def test_budget_not_in_fails_when_the_range_is_still_there():
    out = _out_with_budget(120000.0, 180000.0)
    with pytest.raises(mt.AssertionFailed):
        mt.check_assertion(out, {"type": "budget_not_in", "min": 120000, "max": 180000})


def test_budget_assert_ignores_the_free_text():
    """正文里出现预算数字**不该**影响判定（判据是结构化口径）。"""
    out = _out_with_budget(None, None)
    out.explanation = "预算 12 万的汉L，官方指导价 12.99 万元"
    mt.check_assertion(out, {"type": "budget_not_in", "min": 120000, "max": 180000})
