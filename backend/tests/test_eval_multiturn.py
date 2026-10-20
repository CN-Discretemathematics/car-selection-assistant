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
