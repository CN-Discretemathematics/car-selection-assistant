"""七项产品口径拍板后的落地测试（2026-10-05 用户逐项拍板）。

每个用例都注明：拍板了什么、生产实测的原始现象是什么、断言为什么这么写。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.agent.engine import (
    _priority_phrase,
    _retrieve_chat_evidence,
    next_clarification,
)
from app.agent.schemas import Budget, UserProfile
from app.agent.series_qa import (
    _price_overlap_verdict,
    build_series_qa_answer,
    series_highlights,
)
from tests.seed import (
    make_brand,
    make_series,
    make_source,
    make_variant,
    make_year,
)


def _series_variant_ids(db, series) -> list[int]:
    from sqlalchemy import select

    from app.common.models import VehicleVariant

    return list(
        db.scalars(
            select(VehicleVariant.id).where(
                VehicleVariant.series_id == series.id,
                VehicleVariant.status == "on_sale",
            )
        )
    )


def _mk_fact(variant_id: int, key: str, value: str):
    from app.common.models import SpecFact

    return SpecFact(
        variant_id=variant_id, category="外部配置", fact_key=key, fact_value=value
    )


def _seed(db: Session, name: str, prices: list[str], seat_facts: list[str | None]):
    """造一个车系：多个在售款型，各自指导价与座位事实。"""
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name=f"B-{name}", source=source)
    s = make_series(db, brand, name=name, body_type="sedan",
                    energy_types=("BEV",), source=source)
    variants = []
    for i, price in enumerate(prices):
        y = make_year(db, s, year_name=f"2026款{i}")
        seat = seat_facts[i] if i < len(seat_facts) else None
        facts = []
        if seat:
            facts.append(("外部配置", "记忆泊车", seat, None, None))
        if seat:  # 续航做成三档，用来验 N6-B 的「列全档」
            facts.append(("参数信息", "CLTC纯电续航里程", ["310", "410", "480"][i % 3],
                          "km", "CLTC"))
        variants.append(
            make_variant(db, s, y, config_version=f"配置{i}", price_cny=price,
                         energy_type="BEV", facts=facts, source=source)
        )
    db.commit()
    return s, brand


# ── N8（拍板：只追问，不检索）───────────────────────────────────────────────
def test_no_series_and_no_lock_yields_no_evidence(db_session: Session, monkeypatch):
    """零车系 + 零锁定时**不得检索**。

    实拍：新对话问「它的电池呢」，召回了比亚迪元UP/丰田bZ3/smart精灵#3 三台
    用户一字未提的车，被当作「我资料里的三款」端给用户。
    """
    called: list[str] = []

    def _boom(*a, **k):
        called.append("retrieved")
        return []

    monkeypatch.setattr("app.agent.engine.retrieval_search", _boom)
    assert _retrieve_chat_evidence(db_session, "它的电池呢", [], []) == []
    assert not called, "零车系零锁定时不应发起检索"


def test_no_series_but_locked_still_searches(db_session: Session, monkeypatch):
    """**有**会话锁定时照常检索——那正是上一轮聊过的车，回退路径必须保留。"""
    s, _b = _seed(db_session, "锁定车", ["100000"], [None])
    monkeypatch.setattr("app.agent.engine.retrieval_search",
                        lambda *a, **k: [{"text": "x", "kind": "spec", "source_url": None}])
    out = _retrieve_chat_evidence(db_session, "它的电池呢", [], [s.id])
    assert len(out) == 1, "有锁定车系时不得把证据清空"


# ── N1（拍板：标注覆盖率）───────────────────────────────────────────────────
def test_highlights_annotate_partial_coverage(db_session: Session):
    """6 款里只有 1 款配备记忆泊车 → 必须写明覆盖率，不能说成整系都有。"""
    s, _b = _seed(db_session, "覆盖车", ["100000"] * 6,
                  ["●", "-", "-", "-", "-", "-"])
    out = series_highlights(db_session, s)
    assert any("在售 6 款中 1 款配备" in x for x in out), out


def test_highlights_no_annotation_when_all_variants_equipped(db_session: Session):
    s, _b = _seed(db_session, "全系车", ["100000"] * 3, ["●", "●", "●"])
    out = series_highlights(db_session, s)
    assert any(x == "记忆泊车" for x in out), f"全系配备时不应加括号噪音：{out}"


def test_highlights_excludes_placeholders_only(db_session: Session):
    """全为 `-` 的款型**不算配备**——否则又回到「有这一行就算有」的旧缺陷。"""
    s, _b = _seed(db_session, "占位车", ["100000"] * 3, ["-", "-", "-"])
    assert series_highlights(db_session, s) == []


def test_highlights_does_not_count_optional_as_equipped(db_session: Session):
    """**○（选装）不是配备**（审查实测：真实库里 ○ 的量级接近 ● 的四成）。

    仓库自己的语义表写着 `_FEATURE_VALUE_LABEL = {"●": "有（标配）", "○": "选装"}`，
    却按「不在跳过表里就算配备」统计——于是出现「0 款标配却写『在售 16 款中 2 款配备』」。
    """
    s, _b = _seed(db_session, "选装车", ["100000"] * 4, ["○", "○", "-", "-"])
    assert series_highlights(db_session, s) == [], "选装不得计入配备"


def test_highlights_mixed_standard_and_optional(db_session: Session):
    """混合时只数标配那几款：4 款里 2 款标配。"""
    s, _b = _seed(db_session, "混合车", ["100000"] * 4, ["●", "●", "○", "-"])
    out = series_highlights(db_session, s)
    assert any("在售 4 款中 2 款配备" in x for x in out), out


def test_highlights_counts_variants_not_fact_rows(db_session: Session):
    """同一款同一键**可能有多行重复**（SpecFact 无唯一约束），必须按款型去重。

    否则 n 会超过 total，而标注条件是 `n < total` → 标注被静默关掉，
    N1 要治的病只修掉一部分（审查实测 1841 条亮点无标注）。
    """
    s, _b = _seed(db_session, "重复车", ["100000"] * 3, ["●", "●", "-"])
    # 人为制造重复行：同一 variant + 同一 key 再插一条
    variants = _series_variant_ids(db_session, s)
    db_session.add(_mk_fact(variants[0], "记忆泊车", "●"))
    db_session.add(_mk_fact(variants[0], "记忆泊车", "●"))
    db_session.commit()
    out = series_highlights(db_session, s)
    assert any("在售 3 款中 2 款配备" in x for x in out), f"重复行把 M 抬高了：{out}"


# ── N6-B（拍板：核心参数标「全系极值」+ 参数追问列全档）──────────────────────
def test_single_series_keeps_non_highlight_lines(db_session: Session):
    """核心参数行不依赖 headline——这里断言的是**不带 headline 时不得凭空造行**。

    `series_highlights` 产出的「亮点配置」行不依赖 headline；标题文案由下面那条
    monkeypatch 用例钉死。
    """
    s, brand = _seed(db_session, "口径车", ["310000", "410000", "480000"], [None] * 3)
    text = build_series_qa_answer(db_session, [(s, brand)], "口径车怎么样")
    assert "官方指导价 31.00-48.00 万元" in text, text[:300]


def test_single_series_headline_has_series_extreme_label(db_session: Session, monkeypatch):
    """直接钉住那行标题的文案。

    2026-10-05 订正：N6-B 首版写的是「（最高配）」，被实测证伪——`rank_headlines`
    的口径是逐 label 极值，油耗/加速取 min（最省/最快）恰恰通常是**低配**。
    「汉」会说出「续航 705km（←3 款 EV）+ 油耗 0.67L（←5 款 DM-i）」这种库里不存在的车。
    现按用户拍板改为「（全系极值）」，与极值口径一致且不再暗示单一配置。
    """
    from app.agent import series_qa

    monkeypatch.setattr(series_qa, "series_headline", lambda db, s: {"续航": "310~480 km（CLTC）"})
    s, brand = _seed(db_session, "标注车", ["310000", "410000", "480000"], [None] * 3)
    text = build_series_qa_answer(db_session, [(s, brand)], "标注车续航多少")
    assert "核心参数（全系极值）" in text, text[:300]
    assert "310~480 km（CLTC）" in text, text[:300]
    assert "最高配" not in text, text[:300]


# ── N2（拍板：真的去比价格）─────────────────────────────────────────────────
def test_price_overlap_verdict_detects_overlap(db_session: Session):
    """6.48-9.48 与 6.58-8.68 高度重叠——不能写「差异明显」。"""
    a, _ = _seed(db_session, "车A", ["64800", "94800"], [None, None])
    b, _ = _seed(db_session, "车B", ["65800", "86800"], [None, None])
    v = _price_overlap_verdict(db_session, a, b)
    assert "重叠" in v and "没有重叠" not in v, v


def test_price_overlap_verdict_detects_disjoint(db_session: Session):
    a, _ = _seed(db_session, "车C", ["60000", "70000"], [None, None])
    b, _ = _seed(db_session, "车D", ["300000", "400000"], [None, None])
    assert "没有重叠" in _price_overlap_verdict(db_session, a, b)


def test_price_overlap_verdict_handles_missing_price(db_session: Session):
    """**价格未披露时必须返回空串**，而不是崩。

    这是唯一防 `TypeError: None <= None` 的分支（审查实测：删掉它，全仓仍全绿）。
    """
    a, _ = _seed(db_session, "有价车", ["100000"], [None])
    b, _ = _seed(db_session, "无价车", [None], [None])
    assert _price_overlap_verdict(db_session, a, b) == ""
    assert _price_overlap_verdict(db_session, b, a) == ""
    assert _price_overlap_verdict(db_session, b, b) == ""


def test_comparison_summary_contains_verdict(db_session: Session):
    """**接线**测试：`_price_overlap_verdict` 的结论必须真的落进对比回答里。

    此前测试只直接调该函数，从不检查它是否被接进句子——
    这类「函数正确但没接上」的漏洞只有端到端断言才抓得到。
    """
    a, ba = _seed(db_session, "接线A", ["64800", "94800"], [None, None])
    b, bb = _seed(db_session, "接线B", ["65800", "86800"], [None, None])
    text = build_series_qa_answer(db_session, [(a, ba), (b, bb)], "这两款怎么选")
    assert "价格区间高度重叠" in text, text[-300:]


# ── N4（拍板：全量复述）────────────────────────────────────────────────────
def test_priority_phrase_recites_hard_constraints():
    """品牌 + 预算 + 排除三项**一个都不能漏**（实拍三项全被吞）。"""
    p = UserProfile(budget=Budget(max=300000))
    p.brand_ids = [1]
    p.brand_labels = ["奔驰"]
    p.brand_exclude_ids = [9]
    p.brand_exclude_labels = ["比亚迪"]
    p.avoid = ["电车"]
    p.passengers = 6
    out = _priority_phrase(p)
    for token in ("奔驰", "30 万", "比亚迪", "电车", "6 人"):
        assert token in out, f"回执漏了「{token}」：{out}"


def test_priority_phrase_keeps_exclusion_even_with_positive_brand():
    """**审查实测**：此前 `brand_exclude_ids and not brand_labels` 会让排除项在
    「有正向品牌」时**静默消失**——正是本轮要治的「约束被吞掉」。"""
    p = UserProfile(budget=Budget(max=300000))
    p.brand_ids = [1]
    p.brand_labels = ["奔驰"]
    p.brand_exclude_ids = [9]
    p.brand_exclude_labels = ["比亚迪"]
    assert "比亚迪" in _priority_phrase(p)


def test_priority_phrase_exclusion_absent_when_user_never_excluded():
    """反向钉住：**没说过排除时不得凭空造一个排除项**（回执只许复述，不许发明）。

    断言用 `不要` 出现次数而非「有没有比亚迪」——否则把条件写成
    `brand_exclude_ids or brand_labels`（有正向品牌就进排除分支）会产出一个
    光秃秃的「不要」，而所有「有没有比亚迪」的断言都照样通过（实测存活）。
    """
    p = UserProfile(budget=Budget(max=300000))
    p.brand_ids = [1]
    p.brand_labels = ["奔驰"]
    p.passengers = 6
    out = _priority_phrase(p)
    assert "不要" not in out, f"凭空造出了排除项：{out}"
    assert "比亚迪" not in out


def test_priority_phrase_has_no_useless_placeholder():
    """「不看指定品牌」不指名任何品牌，对用户零信息量——必须消失。"""
    p = UserProfile(budget=Budget(max=300000))
    p.brand_exclude_ids = [9]
    p.brand_exclude_labels = ["比亚迪"]
    out = _priority_phrase(p)
    assert "不看指定品牌" not in out
    assert "比亚迪" in out


def test_priority_phrase_translates_enum_codes():
    """枚举码不得外泄给用户：'suv' → 'SUV'、'BEV' → '纯电'。"""
    p = UserProfile(budget=Budget(max=200000))
    p.body_type = ["suv"]
    p.energy_preference = ["BEV"]
    out = _priority_phrase(p)
    assert "SUV" in out and "suv" not in out.replace("SUV", "")
    assert "纯电" in out and "BEV" not in out


def test_priority_phrase_two_brands_use_and_not_comma():
    """「只看奔驰、宝马」读起来像二选一。"""
    p = UserProfile(budget=Budget(max=200000))
    p.brand_ids = [1, 2]
    p.brand_labels = ["奔驰", "宝马"]
    out = _priority_phrase(p)
    assert "奔驰和宝马" in out


def test_highlights_denominator_is_on_sale_variants(db_session: Session):
    """分母必须是**在售**款型数。

    审查实测：34%（297/877）车系含已停售款，若把停售也算进分母，
    「在售 N 款中 M 款配备」的分母就虚高，标注会大面积失效。
    """
    from app.common.models import VehicleVariant

    s, _b = _seed(db_session, "含停售车", ["100000"] * 2, ["●", "-"])
    variants = _series_variant_ids(db_session, s)
    template = db_session.get(VehicleVariant, variants[0])
    retired = VehicleVariant(
        series_id=s.id, model_year_id=template.model_year_id,
        display_name="停售款", config_version="停售", powertrain="纯电",
        drivetrain="两驱", energy_type="BEV", status="discontinued",
        effective_from=date(2020, 1, 1),
    )
    db_session.add(retired)
    db_session.commit()
    out = series_highlights(db_session, s)
    assert any("在售 2 款中 1 款配备" in x for x in out), f"分母把停售款算进去了：{out}"


def test_variant_label_in_comparison_path_is_marked(db_session: Session, monkeypatch):
    """**对比路径**的「核心参数」也必须标「全系极值」。

    首轮修复只改了单车系 `_describe`，对比路径原样保留，于是同一份数据在
    对比里仍然自相矛盾（审查实测：核心参数 480 km + 下方 310/410/480）。

    注意对比路径的 head 来自 `rank_headlines`（批量），**不是** `series_headline`
    （单车系单查）——monkeypatch 打错对象就会得到一条恒通过的假测试。
    """
    from app.agent import series_qa

    real = series_qa.rank_headlines

    def _fake(facts_by_series):
        out = real(facts_by_series)
        for sid in out:
            out[sid] = {**out[sid], "续航": "310~480 km（CLTC）"}
        return out

    monkeypatch.setattr(series_qa, "rank_headlines", _fake)
    # 必须造事实：`rank_headlines({})` 返回空 → `if head:` 不成立 → 整行不出现，
    # 那样的测试是恒通过的假绿灯（我自己先写了一版就踩了这个坑）。
    a, ba = _seed(db_session, "对比标注车", ["64800", "94800"], ["●", "●"])
    b, bb = _seed(db_session, "对比参照车", ["65800", "86800"], ["●", "●"])
    text = build_series_qa_answer(db_session, [(a, ba), (b, bb)], "这两款怎么选")
    assert "核心参数（全系极值）" in text, text[:400]
    assert "最高配" not in text, text[:400]


def test_priority_phrase_has_no_missing_space_before_enum():
    """真实数据复验发现：`要SUV`（无空格）与 `能源插混`（生硬）是措辞缺陷。

    这类问题合成夹具测不出来——夹具里我只断言了「有没有 SUV」，没断言可读性。
    """
    p = UserProfile(budget=Budget(max=200000))
    p.body_type = ["suv"]
    p.energy_preference = ["PHEV"]
    out = _priority_phrase(p)
    assert "要SUV" not in out and "要BEV" not in out, f"枚举拼接缺空格：{out}"
    assert "要 SUV" in out
    assert "要 插混" in out
    assert "能源插混" not in out, "「能源插混」读起来生硬，应与车身类型同构"


def test_priority_phrase_empty_when_nothing_received():
    assert _priority_phrase(UserProfile()) == ""


def test_clarification_still_asked_after_recite():
    """回执不能取代问题本身。"""
    p = UserProfile(budget=Budget(max=300000))
    out = next_clarification(p)
    assert out is not None
    assert out.missing == ["usage"]
    assert "主要用来跑什么" in out.question, out.question
    assert "已记下：预算不超 30 万" in out.question, out.question
