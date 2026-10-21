"""首要维度 `top_dim`（2026-10-21，计划 §1.2）。

## 计划原文的两条硬约束

1. **「给画像加 `top_dim`，只影响文案组织，不动打分与排序」**
   ⇒ 排序必须**逐位不变**（本文件有专门一条用例钉住）；
2. **「先讲用户最在意的维度，再讲代价」**
   ⇒ 解释里那个维度要**排在最前**并被点名。

## 判据不是「默认权重最高的维度」

默认权重里 `budget` 最高（0.30）——那是**评分口径**，不是用户的偏好。
判据必须是「这一轮被用户**抬高**」的维度。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.engine import extract_hints, merge_profile
from app.agent.schemas import UserProfile
from app.agent.tools import DEFAULT_WEIGHTS
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_cars(db_session: Session) -> None:
    source = make_source(db_session, name="官方测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    for name, price in (("甲车系", "200000"), ("乙车系", "180000")):
        series = make_series(
            db_session, brand, name=name, body_type="sedan",
            energy_types=("BEV",), source=source,
        )
        year = make_year(db_session, series)
        make_variant(
            db_session, series, year, config_version="标准版", energy_type="BEV",
            price_cny=price, source=source,
        )
    db_session.commit()


def _profile(message: str) -> UserProfile:
    """按真实路径构造画像（extract_hints → merge_profile）。"""
    return merge_profile(UserProfile(), extract_hints(message))


def test_top_dim_is_the_raised_dimension():
    """用户抬高哪个维度，首要维度就是哪个。"""
    assert _profile("我最看重动力").top_dim == "power"
    assert _profile("主要看空间").top_dim == "space"


def test_top_dim_is_not_the_default_heaviest_dimension():
    """默认权重里 budget 最高（0.30）——那是评分口径，不是用户偏好。"""
    assert DEFAULT_WEIGHTS["budget"] == max(DEFAULT_WEIGHTS.values())
    assert _profile("我最看重空间").top_dim == "space"
    assert _profile("我最看重空间").top_dim != "budget"


def test_no_emphasis_means_no_top_dim():
    """没说偏好就没有首要维度——常见场景逐位不变。"""
    assert _profile("预算20万，通勤，两三个人").top_dim is None


def test_later_priority_overrides_the_earlier_one():
    """后说的偏好压过先说的（更近的意图更新）。"""
    profile = merge_profile(
        UserProfile(), extract_hints("我最看重动力")
    )
    profile = merge_profile(profile, extract_hints("我最看重空间"))
    assert profile.top_dim == "space"


def test_top_dim_does_not_change_the_ranking(db_session: Session):
    """**不动打分与排序**（计划原文的硬约束）。

    只差 `top_dim` 这**一个字段**（权重完全相同）时，分数与顺序必须逐位相同——
    比的是**分数**而不只是顺序：顺序可能在权重相同时恰好不变，分数不会。
    """
    from app.agent.tools import recommendation_tool

    _seed_cars(db_session)
    raised = merge_profile(
        merge_profile(UserProfile(), extract_hints("预算20万，通勤，两三个人")),
        extract_hints("我最看重动力"),
    )
    assert raised.top_dim == "power"
    without = raised.model_copy(update={"top_dim": None})

    a = recommendation_tool(db_session, without)
    b = recommendation_tool(db_session, raised)
    assert [(v["variant_id"], v["score"]) for v in a["variants"]] == [
        (v["variant_id"], v["score"]) for v in b["variants"]
    ], "首要维度只影响文案组织，不得改变打分与排序"
    assert a["count"] == b["count"]


def test_writing_top_dim_does_not_touch_the_weights():
    """首要维度是**画像的一个字段**——写它不得顺手改权重（那就是在动打分）。

    权重是**绝对值**语义（`profile.weights` 覆盖默认），所以这里直接比对
    `extract_hints` 给出的那份，而不是另写一份魔法数字（避免口径漂移）。
    """
    hints = extract_hints("我最看重动力")
    profile = merge_profile(UserProfile(), hints)
    assert profile.top_dim == "power"
    assert profile.weights == hints["weights"], (profile.weights, hints["weights"])


def test_explanation_names_the_top_dimension_first(db_session: Session):
    """有首要维度时，解释里它被点名且排在匹配项最前。"""
    from app.agent.engine import AgentEngine
    from app.agent.tools import recommendation_tool

    _seed_cars(db_session)
    profile = merge_profile(
        merge_profile(UserProfile(), extract_hints("预算20万，通勤，两三个人")),
        extract_hints("我最看重动力"),
    )
    result = recommendation_tool(db_session, profile)
    text = AgentEngine._template_explanation(profile, result, [], [])
    assert "按你最看重的动力排的" in text, text
    assert text.index("动力") < text.index("妥协项"), f"首要维度应先于代价出现：{text}"


def test_explanation_unchanged_without_top_dim(db_session: Session):
    """没有首要维度时，文案**一个字都不多**。"""
    from app.agent.engine import AgentEngine
    from app.agent.tools import recommendation_tool

    _seed_cars(db_session)
    profile = merge_profile(UserProfile(), extract_hints("预算20万，通勤，两三个人"))
    result = recommendation_tool(db_session, profile)
    text = AgentEngine._template_explanation(profile, result, [], [])
    assert "按你最看重的" not in text, text
    assert "匹配项：" in text, text


def test_llm_prompt_leads_with_the_top_dimension():
    """解释模块的提示必须点名首要维度并要求**先讲**。

    这句提示此前内联在 `_explain` 里，**没法单测**——改掉了也没有用例变红。
    现在抽成纯函数 `_explain_system_prompt`，提示内容被用例直接钉住。
    """
    from app.agent.engine import _explain_system_prompt

    profile = merge_profile(UserProfile(), extract_hints("我最看重动力"))
    prompt = _explain_system_prompt(profile)
    assert "动力" in prompt, prompt
    assert "先讲" in prompt, prompt

    # 没有首要维度 → 提示**一个字都不加**（常见场景逐位不变）
    plain = _explain_system_prompt(
        merge_profile(UserProfile(), extract_hints("预算20万，通勤"))
    )
    assert "最在意" not in plain, plain
    assert "先讲" not in plain, plain


def test_match_items_never_claim_an_unverified_dimension(db_session: Session):
    """「匹配项」是这台车**已验证达标**的项——不得替它认领没核实过的维度。

    首要维度只是**用户在意**的方向，库里可能压根没有这个维度的数据
    （种子车没有功率事实 → `dims["power"]` 是 0.5 中性，不是「弱」，更不是「匹配」）。
    把首要维度硬塞进匹配项＝编造（设计原则第 1 条）。
    """
    from app.agent.engine import AgentEngine
    from app.agent.tools import recommendation_tool

    _seed_cars(db_session)
    profile = merge_profile(
        merge_profile(UserProfile(), extract_hints("预算20万，通勤，两三个人")),
        extract_hints("我最看重动力"),
    )
    result = recommendation_tool(db_session, profile)
    text = AgentEngine._template_explanation(profile, result, [], [])

    matched_part = text.split("匹配项：", 1)[1].split("）。", 1)[0]
    assert "动力" not in matched_part, text
    for variant in result["variants"]:
        assert not any("动力" in item for item in variant["matched"]), variant["matched"]
