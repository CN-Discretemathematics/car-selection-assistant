"""多轮题库**生成器**的守卫（2026-10-21）。

多轮框架的头号风险是「构造的前提让被测行为压根不发生」——题库生成器正是那个前提：

- 旧生成器用**硬编码**的品牌/车身/能源组合拼需求句 ⇒ 库里未必有这种车
  （「埃安的轿车」——埃安没有轿车）⇒ 引擎**出不了推荐**；
- 旧生成器的需求句**缺「用途」**⇒ 引擎判定缺项，只反问「主要用途是什么？」，
  **从不推荐** ⇒ `not_recommended` 断言**恒真**。

实测：旧生成器的 50 道题全部被判「无推荐→断言恒真」，**一道都没真跑起来**。

⇒ 这里的用例把「需求句必须让引擎真的推荐」钉住。
"""
from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gen_multiturn_scenarios as gen  # noqa: E402

from app.agent.engine import AgentEngine, extract_hints  # noqa: E402
from app.agent.session import SessionStore  # noqa: E402
from app.common.llm import LLMClient  # noqa: E402
from tests.seed import make_brand, make_series, make_source, make_variant, make_year  # noqa: E402


def _seed(db_session) -> None:
    """种八个**符合各档**的车系：比亚迪 · 轿车 · 纯电 · 15 万。

    ⚠️ 为什么是**八个**：拒绝场景要三轮都能验到东西——
    ① 第一轮推得出 ≥2 个车系（否则拒绝之后候选集空了 ⇒ 第三轮「被拒那台不在榜」是白送的）；
    ② 追问「再推荐几款」之后**还有新的一批**（生成器会照搬三轮校验这一条）——
    一轮展示 5 台 + 拒绝轮再展示 4 台 ⇒ 已展示的车系约 5 个 ⇒ 池子至少要 6 个车系。

    ⚠️ 这条校验**依赖引擎能力**：引擎没有「排除已展示过的车系」时会被跳过
    （见 `gen_multiturn_scenarios._engine_supports_shown_exclusion`），
    那时种两个车系就够——但多种几个对两种情形都安全 ✓。
    """
    source = make_source(db_session, name="生成器测试来源")
    brand = make_brand(db_session, name="比亚迪", source=source)
    for i in range(8):
        series = make_series(
            db_session, brand, name=f"测试车系{i}", body_type="sedan",
            energy_types=("BEV",), source=source,
        )
        year = make_year(db_session, series)
        make_variant(
            db_session, series, year, config_version="标准版", energy_type="BEV",
            price_cny="150000", source=source,
        )
    db_session.commit()


def test_sampled_requirement_is_satisfiable(db_session):
    """抽到的需求必须**让引擎真的推荐**（不是反问）——这是断言有意义的前提。"""
    _seed(db_session)
    requirements = gen.sample_requirements(db_session, random.Random(7), 3)
    assert requirements, "库里明明有在售车系，却一个都没抽到"

    engine = AgentEngine(llm=LLMClient(api_key=None), store=SessionStore())
    for req in requirements:
        turn = gen._recommend_turn(req)
        out = asyncio.run(engine.handle(db_session, engine._store.create(), turn))
        assert out.recommended_variants, f"这句没出推荐（引擎在反问）：{turn}"


def test_sampled_requirement_needs_multiple_candidates(db_session):
    """抽到的需求必须**第一轮推得出 ≥2 个车系**。

    只有一台车时，拒绝之后候选集就空了 ⇒ 第三轮「被拒那台不在榜」是**白送的**
    （推荐列表空 ⇒ 那台车当然不在）⇒ 场景通过但什么都没验。
    实测：单候选需求下，拒绝功能**整个缺失**的分支仍有 17/50「碰巧通过」。
    """
    _seed(db_session)
    requirements = gen.sample_requirements(db_session, random.Random(7), 3)
    assert requirements, "库里明明有在售车系，却一个都没抽到"
    for req in requirements:
        assert len(req.get("first_turn_series") or []) >= 2, req


def test_requirement_turn_covers_every_required_dimension(db_session):
    """需求句必须带齐引擎的必需项：预算 / 用途 / 人数（外加品牌、车身、能源）。"""
    _seed(db_session)
    requirements = gen.sample_requirements(db_session, random.Random(7), 3)
    assert requirements
    for req in requirements:
        hints = extract_hints(gen._recommend_turn(req))
        assert hints.get("budget"), req
        assert hints.get("usage"), f"缺用途（引擎会反问）：{gen._recommend_turn(req)}"
        assert hints.get("passengers") is not None, req
        assert hints.get("body_type"), req
        assert hints.get("energy_preference"), req


def test_rejected_car_is_the_sampled_series(db_session):
    """被拒的那台必须**就是需求里那台**——否则「它不该再被推荐」恒真（通过但什么都没验）。"""
    _seed(db_session)
    requirements = gen.sample_requirements(db_session, random.Random(7), 2)
    scenarios = gen.gen_reject_scenarios(random.Random(11), requirements)
    for scenario, req in zip(scenarios, requirements, strict=True):
        assert scenario["turns"][1]["user"].startswith(req["series_name"]), scenario
        assert scenario["turns"][2]["assert"] == [
            {"type": "not_recommended", "car": req["series_name"]}
        ], scenario


def test_rejection_remembered_scenario_shape(db_session):
    """场景 4（拒绝跨会话记住）的形状 ✓。

    三个要点：
    - **两个会话**（`first` / `second`）——同一场里换会话才是「跨会话」✓；
    - 第二轮是**拒绝**那一轮 ✓；
    - 第三轮断言那台车**不再出现** ✓（且被拒的车必须是需求里那台 ⇒ 不恒真 ✓）。
    """
    _seed(db_session)
    rng = random.Random(5)
    requirements = gen.sample_requirements(db_session, rng, 2)
    scenarios = gen.gen_rejection_remembered_scenarios(rng, requirements, enabled=True)
    assert scenarios
    for scenario, req in zip(scenarios, requirements, strict=True):
        turns = scenario["turns"]
        assert scenario["scenario"] == "rejection_remembered_across_sessions"
        assert [t.get("session") for t in turns] == ["first", "first", "second"]
        assert turns[1]["user"].startswith(req["series_name"]), scenario
        assert turns[2]["assert"] == [
            {"type": "not_recommended", "car": req["series_name"]}
        ], scenario


def test_rejection_remembered_scenario_is_disabled_by_default(db_session):
    """默认关（依赖 Phase 2b）✓ —— 没合入时跑了必然红，不该混进分母。"""
    _seed(db_session)
    rng = random.Random(5)
    requirements = gen.sample_requirements(db_session, rng, 1)
    scenarios = gen.gen_rejection_remembered_scenarios(rng, requirements)
    assert scenarios[0]["enabled"] is False
    assert scenarios[0].get("reason_disabled")


def test_memory_deleted_scenario_has_a_positive_control(db_session):
    """「删记忆」场景必须**先**在另一个会话验过「记忆还在」。

    否则它分不清「删掉了」和「本来就没记住」——
    实测：记忆功能整体关闭（`DURABLE_PREFS_MODE=off`）时，旧版场景**照样 50/50 通过**
    ⇒ 那是个恒真的断言。有对照轮后，记忆功能一关，对照轮必红。
    """
    _seed(db_session)
    rng = random.Random(5)
    requirements = gen.sample_requirements(db_session, rng, 2)
    scenarios = gen.gen_memory_deleted_scenarios(rng, requirements)
    for scenario, req in zip(scenarios, requirements, strict=True):
        turns = scenario["turns"]
        lo, hi = req["band"]
        lo, hi = lo * 10000, hi * 10000
        asserts = [a for t in turns for a in t["assert"]]
        assert {"type": "budget_in", "min": lo, "max": hi} in asserts, scenario
        assert {"type": "budget_not_in", "min": lo, "max": hi} in asserts, scenario
        # 对照轮（budget_in）必须在删除轮**之前**
        in_idx = next(
            i for i, t in enumerate(turns)
            if any(a["type"] == "budget_in" for a in t["assert"])
        )
        forget_idx = next(i for i, t in enumerate(turns) if "忘掉" in t["user"])
        assert in_idx < forget_idx, scenario
        # 三个会话：陈述 → 跨会话对照+删除 → 删除后再换会话
        assert len({t["session"] for t in turns}) == 3, scenario


def test_memory_scenarios_are_disabled_by_default_and_flippable(db_session):
    """两个记忆场景**默认关**（依赖未落地能力），开关能打开。"""
    _seed(db_session)
    rng = random.Random(9)
    requirements = gen.sample_requirements(db_session, rng, 1)

    off = gen.gen_returning_scenarios(rng, requirements) + gen.gen_memory_deleted_scenarios(
        rng, requirements
    )
    assert all(s["enabled"] is False for s in off), off
    assert all(s.get("reason_disabled") for s in off), off

    on = gen.gen_returning_scenarios(
        rng, requirements, enabled=True
    ) + gen.gen_memory_deleted_scenarios(rng, requirements, enabled=True)
    assert all(s["enabled"] is True for s in on), on


def test_reject_scenario_asserts_the_rejection_takes_effect_immediately(db_session):
    """拒绝**当轮**就要断言「它不在推荐里」。

    否则拒绝功能整个缺失时，那台车只要**碰巧**掉出榜单就算通过
    （实测：分支 7 单独跑 23/50「碰巧通过」，加上这条断言后 0/50）。
    """
    _seed(db_session)
    requirements = gen.sample_requirements(db_session, random.Random(11), 2)
    scenarios = gen.gen_reject_scenarios(random.Random(11), requirements)
    for scenario, req in zip(scenarios, requirements, strict=True):
        reject_turn = scenario["turns"][1]
        assert reject_turn["user"].startswith(req["series_name"]), scenario
        assert reject_turn["assert"] == [
            {"type": "not_recommended", "car": req["series_name"]}
        ], scenario


def test_generator_emits_three_scenarios_with_the_requested_count(db_session):
    _seed(db_session)
    rng = random.Random(3)
    requirements = gen.sample_requirements(db_session, rng, 2)
    scenarios = (
        gen.gen_reject_scenarios(rng, requirements)
        + gen.gen_returning_scenarios(rng, requirements)
        + gen.gen_memory_deleted_scenarios(rng, requirements)
    )
    assert len(scenarios) == 3 * len(requirements)
    names = {s["scenario"] for s in scenarios}
    assert names == {
        "reject_then_no_recommend",
        "returning_user_memory",
        "memory_deleted",
    }, names
    # 两个记忆场景仍标 enabled=false（依赖尚未上线的跨会话能力）
    for scenario in scenarios:
        if scenario["scenario"] != "reject_then_no_recommend":
            assert scenario["enabled"] is False, scenario
            assert scenario.get("reason_disabled"), scenario


def test_exclusion_memory_scenario_shape_and_is_not_vacuous(db_session):
    """场景 5（**排除项**跨会话记住）的形状 + **非恒真**。

    ## 为什么单独立一条

    计划 §七的第一条判断是「**硬约束排除项**是记忆的第一价值」，
    而在此之前跨会话题库里**没有这一类** ⇒ 那条判断在评测层面是空的。

    ## 「非恒真」是什么意思

    「该品牌不该再出现」在**没有记忆**时也可能为真——如果那个品牌**本来就不在榜**。
    所以生成器必须**先验一遍**：不带排除项时，那个品牌的车**确实在推荐里**才收。
    这条用例把那个前提**钉住**：它重跑一遍推荐，确认品牌确实在榜。
    """
    _seed(db_session)
    rng = random.Random(5)
    requirements = gen.sample_requirements(db_session, rng, 3)
    scenarios = gen.gen_exclusion_memory_scenarios(
        rng, requirements, db_session, enabled=True
    )
    assert scenarios, "一个都没生成（种子库里至少有一个品牌的车该在榜）"
    for scenario in scenarios:
        turns = scenario["turns"]
        assert scenario["scenario"] == "exclusion_memory", scenario
        assert scenario["enabled"] is True, scenario
        # 两个会话：第一轮说排除项，第二轮换会话问 ⇒ 才是「跨会话」
        assert [t.get("session") for t in turns] == ["first", "second"], scenario
        brand = turns[1]["assert"][0]["brand"]
        assert turns[1]["assert"] == [
            {"type": "brand_not_recommended", "brand": brand}
        ], scenario
        assert brand in turns[0]["user"], scenario
        # 非恒真：那个品牌**本来就在榜**（否则断言白送）
        base_turn = turns[0]["user"].replace(f"，不要{brand}", "")
        pairs = gen._recommended_series_brands(db_session, base_turn)
        assert any(b == brand for _name, b in pairs), (
            f"「{brand}」本来就不在推荐里 ⇒ 断言恒真（这个场景什么都没验）"
        )


def test_exclusion_memory_scenario_is_disabled_by_default(db_session):
    """默认关（依赖跨会话记忆）✓ —— 没合入时跑了必然红，不该混进分母。"""
    _seed(db_session)
    rng = random.Random(5)
    requirements = gen.sample_requirements(db_session, rng, 2)
    scenarios = gen.gen_exclusion_memory_scenarios(rng, requirements, db_session)
    for scenario in scenarios:
        assert scenario["enabled"] is False, scenario
        assert scenario.get("reason_disabled"), scenario
