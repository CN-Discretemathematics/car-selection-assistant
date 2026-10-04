"""N3 / N5：车系档案与版本差异表的两个实测缺陷（2026-10-05 生产实测）。

N5：单车系档案里车系名**同一句出现两次**
    关于「东风奕派eπ007」：
    「东风奕派eπ007」：中大型车 · 轿车 · …

N3：版本差异表把**配置项**与**硬参数**混在一张表里
    实拍：续航｜电池能量｜电动机总功率｜后电动机最大扭矩｜电池快充时间(分钟)｜
    电池快充时间(小时)｜记忆泊车｜辅助泊车入位｜主/副驾驶座电动调节｜后座出风口｜
    后电动机型号 TZ160XS001｜整备质量
    想找「哪个版本有记忆泊车」得在一堆 kW·min·kg 里翻。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.series_qa import build_series_qa_answer, build_variant_diff_answer
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_series(db: Session):
    """星愿式样例：3 个配置、参数与配置项都有差异。"""
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)
    s = make_series(db, brand, name="测试007", body_type="sedan",
                    energy_types=("BEV",), source=source)
    plan = [
        ("低配", "100000", [
            ("参数信息", "纯电续航里程", "310", "km", "CLTC"),
            ("参数信息", "电动机总功率", "58", "kW", None),
            ("外部配置", "记忆泊车", "-", None, None),
        ]),
        ("中配", "140000", [
            ("参数信息", "纯电续航里程", "410", "km", "CLTC"),
            ("参数信息", "电动机总功率", "85", "kW", None),
            ("外部配置", "记忆泊车", "●", None, None),
        ]),
        ("高配", "180000", [
            ("参数信息", "纯电续航里程", "480", "km", "CLTC"),
            ("参数信息", "电动机总功率", "85", "kW", None),
            ("外部配置", "记忆泊车", "●", None, None),
        ]),
    ]
    for cfg, price, facts in plan:
        y = make_year(db, s, year_name=f"{cfg}款")
        make_variant(db, s, y, config_version=cfg, price_cny=price,
                     energy_type="BEV", facts=facts, source=source)
    db.commit()
    return s, brand


# ── N5 ──────────────────────────────────────────────────────────────────────
def test_single_series_archive_does_not_repeat_name(db_session: Session):
    """`_describe` 本身就以全名开头，外层再套一次 `关于「X」：` 就重复了。

    ⚠️ 必须横跨**前两行**计数：重复形如
        关于「X」：
        「X」：…
    只查第一行的话，`关于「X」：` 里只出现一次 → 断言恒绿 → **假绿灯**
    （反向验证实测：把修复改回去，这条测试当时确实没红）。
    """
    s, brand = _seed_series(db_session)
    text = build_series_qa_answer(db_session, [(s, brand)], "测试007怎么样")
    head_block = "\n".join(text.splitlines()[:2])
    assert head_block.count("测试007") == 1, f"车系名在头部重复出现：\n{head_block}"


def test_single_series_archive_still_names_the_series(db_session: Session):
    """去掉重复不等于去掉名字——首行仍必须点明是哪台车。"""
    s, brand = _seed_series(db_session)
    text = build_series_qa_answer(db_session, [(s, brand)], "测试007怎么样")
    assert "测试007" in text.splitlines()[0]


# ── N3 ──────────────────────────────────────────────────────────────────────
def test_variant_diff_separates_config_from_params(db_session: Session):
    """配置项与硬参数必须分块列出，不能混在一张表里。"""
    s, brand = _seed_series(db_session)
    text, _ = build_variant_diff_answer(db_session, s, brand, "不同版本有什么区别")

    assert "版本差异·配置" in text, "配置块缺失"
    assert "版本差异·参数" in text, "参数块缺失"
    # 记忆泊车属配置、纯电续航里程属参数，必须落在不同块里
    cfg_idx = text.index("版本差异·配置")
    para_idx = text.index("版本差异·参数")
    mem_idx = text.index("记忆泊车")
    range_idx = text.index("纯电续航里程")
    assert cfg_idx < mem_idx < para_idx < range_idx, "配置与参数未按块分列"


def test_variant_diff_config_block_lists_own_items(db_session: Session):
    s, brand = _seed_series(db_session)
    text, _ = build_variant_diff_answer(db_session, s, brand, "不同版本有什么区别")
    cfg_block = text[text.index("版本差异·配置"):text.index("版本差异·参数")]
    assert "记忆泊车" in cfg_block
    assert "纯电续航里程" not in cfg_block, "参数项混进了配置块"


def test_variant_diff_param_block_excludes_config_items(db_session: Session):
    s, brand = _seed_series(db_session)
    text, _ = build_variant_diff_answer(db_session, s, brand, "不同版本有什么区别")
    para_block = text[text.index("版本差异·参数"):]
    assert "纯电续航里程" in para_block
    assert "记忆泊车" not in para_block, "配置项混进了参数块"


def test_variant_diff_keeps_single_block_when_only_one_kind(db_session: Session):
    """只有一类差异时**不该**多出一个空标题块。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="纯参数品牌", source=source)
    s = make_series(db_session, brand, name="只有参数差异", body_type="sedan",
                    energy_types=("BEV",), source=source)
    for cfg, price, rng in (("A", "100000", "310"), ("B", "140000", "480")):
        y = make_year(db_session, s, year_name=f"{cfg}款")
        make_variant(db_session, s, y, config_version=cfg, price_cny=price,
                     energy_type="BEV",
                     facts=[("参数信息", "纯电续航里程", rng, "km", "CLTC")],
                     source=source)
    db_session.commit()

    text, _ = build_variant_diff_answer(db_session, s, brand, "不同版本有什么区别")
    assert "版本差异·参数" in text
    assert "版本差异·配置" not in text, "无配置差异时不应出现空配置块"
