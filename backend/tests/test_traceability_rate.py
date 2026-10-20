"""可追问率的口径守卫（计划 §5.1）。

这些用例守的是**判据本身**：
- 数字提取不能被千分位拆错（否则真实出处被误判成没有出处）；
- 依据集合**不能含被测正文**（否则「正文里的数字在正文里」恒真，指标当场作废）；
- 没有出处的数字必须判成 `none`，不许被别的桶兜住；
- 库内事实里的数字要有出处（带 source_id）——那是「系统能自动给出出处」的真凭据。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import eval_traceability as et  # noqa: E402

from tests.seed import make_brand, make_series, make_source, make_variant, make_year  # noqa: E402


def _card(display_name: str = "汉L 2025款 DM-p", price: float = 199800.0, **extra) -> dict:
    card = {
        "variant_id": 101,
        "series_id": 11,
        "series_name": "汉L",
        "brand_name": "比亚迪",
        "display_name": display_name,
        "energy_type": "PHEV",
        "price_cny": price,
        "score": 0.9783,
    }
    card.update(extra)
    return card


def test_thousands_separator_is_one_number():
    """`199,800` 是**一个**数——按 `\\d+` 切会切成 199 与 800 两个。"""
    assert et.numbers_in("售价199,800元，续航701KM") == [199800.0, 701.0]


def test_unit_scale_numbers_are_not_data_values():
    """`L/100km`、`0-100km/h` 里的数字是**单位刻度**，不是数据值。

    不排除它们，「油耗 6.35 L/100km」里的 100 会被判成「没有出处」——
    实测 30 题里 14 题踩这个坑。
    """
    assert et.numbers_in("油耗 6.35 L/100km") == [6.35]
    assert et.numbers_in("加速 0-100km/h") == []
    # 半角 en dash + 空格（实测踩过：模型写「0–100 km/h」，lookahead 没留空格就会漏）
    assert et.numbers_in("加速 0–100 km/h 为 7.7 s") == [7.7]


def test_example_numbers_are_not_claims():
    """`比如「预算15万，家用5口人」` 里的数字是**假设**，不是事实主张。"""
    assert et.numbers_in("比如「预算15万，家用5口人，想要新能源SUV」") == []


def test_card_number_is_traceable_to_the_card():
    """卡片里的价格（含万元口径）有出处。"""
    basis = et.build_basis(variants=[_card()], citations=[], profile={}, user_text="")
    rows = et.trace_text("官方指导价 19.98 万元", basis)
    assert rows and rows[0]["kind"] == et.KIND_SOURCE, rows
    assert "汉L" in rows[0]["label"], rows


def test_user_stated_number_is_traceable_to_the_user():
    """用户自己说过的数字（预算）算 `user` 桶，不是没有出处。"""
    basis = et.build_basis(
        variants=[_card()],
        citations=[],
        profile={"budget": {"max": 200000.0}, "passengers": 3},
        user_text="预算20万左右",
    )
    rows = et.trace_text("预算 20 万元以内", basis)
    assert rows and rows[0]["kind"] == et.KIND_USER, rows


def test_invented_number_is_not_traceable():
    """库里没有的数字必须判成 `none`——**哪怕它就出现在被测正文里**。

    这一条同时是「依据集合不含正文」的守卫：若把正文算进依据集合，
    这条会立刻变绿（= 指标自证作废）。
    """
    basis = et.build_basis(variants=[_card()], citations=[], profile={}, user_text="")
    rows = et.trace_text("续航 800 公里", basis)
    assert [r["kind"] for r in rows] == [et.KIND_NONE], rows


def test_count_numbers_are_derived():
    """计数类数字（「共 12 款」）的出处是**查询条件**，本工具不查库 → 记 `derived`。"""
    basis = et.build_basis(variants=[_card()], citations=[], profile={}, user_text="")
    rows = et.trace_text("共 12 款在售款型", basis)
    assert rows and rows[0]["kind"] == et.KIND_DERIVED, rows


def test_sentence_rate_only_counts_sentences_with_numbers():
    """不含数字的句子不提出可追问主张，不进分母。"""
    basis = et.build_basis(variants=[_card()], citations=[], profile={}, user_text="")
    rep = et.report("为你推荐汉L。这台车续航 800 公里。", basis)
    assert rep["sentences_scored"] == 1, rep
    assert rep["sentences_traceable"] == 0, rep
    assert rep["sentence_rate"] == 0.0, rep


def test_report_counts_four_buckets():
    basis = et.build_basis(
        variants=[_card()],
        citations=[{"source_id": 1, "source_name": "汽车之家"}],
        profile={"budget": {"max": 200000.0}},
        user_text="预算20万",
    )
    rep = et.report("预算 20 万，官方指导价 19.98 万元，续航 800 公里，来源 1。", basis)
    assert rep["numbers"] == 4, rep
    assert rep["by_kind"][et.KIND_NONE] == 1, rep
    assert rep["numbers_traceable"] == 3, rep
    assert rep["number_rate"] == 0.75, rep


def test_yuan_derivation_does_not_invent_small_numbers():
    """万元派生只对 ≥10000 的值生效——否则 50000 元会派生出「5」，把款数糊弄成有出处。"""
    basis = et.Basis()
    basis.add(9.0, et.KIND_SOURCE, "某个小数字")
    assert basis.lookup(0.0009) is None
    basis.add(199800.0, et.KIND_SOURCE, "价格")
    assert basis.lookup(19.98) is not None


def test_list_items_are_collected():
    """**列表里的标量也要收**——`locked_series_ids: [905]` 里的 905 是有出处的。

    口径自己的 bug：第一版遍历时把列表元素整个丢掉，于是画像里的内部 id
    没进依据集合 → 用户可见文案里的「车系 ID 905」被判成「没有出处」。
    """
    basis = et.build_basis(
        variants=[],
        citations=[],
        profile={"locked_series_ids": [905], "must_have": ["座椅通风", "12V电源"]},
        user_text="",
    )
    assert basis.lookup(905) is not None
    assert basis.lookup(12) is not None


def test_database_facts_are_traceable_with_a_source(db_session):
    """库内参数事实里的数字要有出处，且出处带 source_id。

    这是「系统能自动给出出处」的真凭据——参数问答路径的答案数字全部来自这里。
    """
    source = make_source(db_session, name="测试来源")
    brand = make_brand(db_session, name="测试品牌", source=source)
    series = make_series(db_session, brand, name="参数车系", source=source)
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, price_cny="188000",
        facts=[("动力", "最大功率(kW)", "280", "kW", None)],
        source=source,
    )
    db_session.commit()

    basis = et.build_basis(
        db=db_session,
        variants=[],
        citations=[],
        profile={},
        user_text="",
        answer_text="参数车系 最大功率 280 kW",
    )
    rows = et.trace_text("最大功率 280 kW，官方指导价 18.8 万元", basis)
    kinds = [r["kind"] for r in rows]
    assert kinds == [et.KIND_SOURCE, et.KIND_SOURCE], rows
    assert "来源" in rows[0]["label"], rows
