"""三项用户拍板的落地测试（2026-10-05 端到端复验时发现）。

1. **参数行不重复单位**：`probe_facts` 渲染时剥掉尾部**确实是单位**的括号。
   此前用户看到「轴距(mm) = 2650 mm」——单位在标签和值上各写一遍，
   实测 877 车系 × 10 组提问 32347 条渲染行里 **47.9%** 是这个形态。
2. **尺寸多档时取众数 + 覆盖率**：此前是 text 模式「取首值」，那个首值取决于
   数据库返回顺序，本质是**任取**（实测 312/877 = 35.6% 的车系尺寸多档）。
3. **RAG 车系摘要切片必须带 label**：此前 `head_parts` 只取值、丢掉 label，
   切片读起来是「核心参数：2650 mm；58~85 kW」——分不清哪个是轴距、哪个是动力。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.series_qa import display_fact_key, probe_facts, size_line
from app.catalog.series_index import HEADLINE_PREFIX
from app.rag.ingest import _summary_chunk_text
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

F_SIZE = "长*宽*高(mm)"
F_RANGE = "CLTC综合续航(km)"
F_TRUNK = "前备厢容积(L)"


# ── 1. 参数行单位 ─────────────────────────────────────────────────────────
def test_ascii_unit_parens_stripped():
    assert display_fact_key("轴距(mm)") == "轴距"
    assert display_fact_key("前备厢容积(L)") == "前备厢容积"
    assert display_fact_key("CLTC综合油耗(L/100km)") == "CLTC综合油耗"
    assert display_fact_key("官方0-100km/h加速(s)") == "官方0-100km/h加速"
    assert display_fact_key("电池能量(kWh)") == "电池能量"


def test_chinese_unit_parens_stripped():
    """`unit_from_key` 只认 ASCII，「电池快充时间(小时)」它认不出「小时」——这里补。"""
    assert display_fact_key("电池快充时间(小时)") == "电池快充时间"
    assert display_fact_key("电池快充时间(分钟)") == "电池快充时间"


def test_name_parens_preserved():
    """括号里是**名字的一部分**时必须原样保留，剥掉就丢了真实信息。"""
    for key in (
        "540°全景影像系统(带透明底盘)",
        "540°智能影像（透明底盘）",
        "M碳陶瓷高性能卡钳（金色卡钳）",
        "L2级组合驾驶辅助包（限时免费）",
        "AI空气投影（限时1500）",
    ):
        assert display_fact_key(key) == key, key


def test_key_without_tail_parens_untouched():
    assert display_fact_key("19寸马牌静音轮胎") == "19寸马牌静音轮胎"
    assert display_fact_key("空气悬架") == "空气悬架"
    assert display_fact_key("") == ""


def test_probe_output_has_no_duplicated_unit():
    """端到端：`probe_facts` 渲染出的行里，单位不得在标签和值上各出现一次。"""
    facts = [
        (F_SIZE, "4135*1805*1570", "mm", None),
        (F_RANGE, "310", "km", "CLTC"),
        (F_TRUNK, "70", "L", None),
    ]
    lines = probe_facts(facts, "续航和空间尺寸")
    joined = "；".join(lines)
    assert "轴距(mm)" not in joined
    assert "前备厢容积(L)" not in joined
    assert "轴距(mm) = " not in joined
    assert any(line.startswith("前备厢容积 = ") for line in lines), lines
    # 值上的单位仍在
    assert any("70 L" in line for line in lines), lines


# ── 2. 尺寸众数 + 覆盖率 ─────────────────────────────────────────────────
def _seed_sizes(db: Session, name: str, sizes: list[str]):
    brand = make_brand(db, f"尺寸品牌{name}")
    series = make_series(db, brand, name)
    year = make_year(db, series)
    source = make_source(db)
    for index, size in enumerate(sizes):
        make_variant(
            db, series, year,
            config_version=f"配置{index}",
            price_cny=100000 + index * 1000,
            facts=[("尺寸", F_SIZE, size, "mm", None)],
            source=source,
        )
    return series


def test_size_single_tier_has_no_coverage_note(db_session: Session):
    """单档时照旧，不加覆盖率——加了对 565/877 的车系是纯噪声。"""
    series = _seed_sizes(db_session, "单档", ["4135*1805*1570"] * 3)
    assert size_line(db_session, series) == "4135*1805*1570 mm"


def test_size_multi_tier_uses_mode_with_coverage(db_session: Session):
    """多档时取众数并标覆盖率——首值是任取的，众数才是有代表性的那个。"""
    series = _seed_sizes(
        db_session, "多档", ["4997*1963*1445", "4997*1963*1445", "4997*1963*1460"]
    )
    assert (
        size_line(db_session, series)
        == "4997*1963*1445 mm（在售 3 款中 2 款为此尺寸）"
    )


def test_size_mode_ignores_insertion_order(db_session: Session):
    """众数不该被「第一条事实是谁」左右：两种插入顺序给出同一个答案。"""
    forward = _seed_sizes(
        db_session, "顺序甲", ["4997*1963*1445", "4997*1963*1460", "4997*1963*1445"]
    )
    assert "1445" in (size_line(db_session, forward) or "")

    backward = _seed_sizes(
        db_session, "顺序乙", ["4997*1963*1460", "4997*1963*1445", "4997*1963*1445"]
    )
    assert "1445" in (size_line(db_session, backward) or "")


def test_size_tie_keeps_first_and_still_discloses(db_session: Session):
    """并列第一时取先出现的那个，**但覆盖率必须照说**——不能让人以为全系如此。"""
    series = _seed_sizes(
        db_session, "并列", ["4997*1963*1445", "4997*1963*1460"]
    )
    text = size_line(db_session, series)
    assert text is not None
    assert "在售 2 款中 1 款为此尺寸" in text, text


def test_size_absent_returns_none(db_session: Session):
    """没有尺寸事实时返回 None——不得凭空造一个尺寸。"""
    brand = make_brand(db_session, "无尺寸品牌")
    series = make_series(db_session, brand, "无尺寸车")
    assert size_line(db_session, series) is None


def test_size_counts_by_variant_not_by_fact_row(db_session: Session):
    """**按款型**去重计数：N1 吃过一次亏——`SpecFact` 对 `(variant_id, fact_key)`
    无唯一约束，按事实行累加会虚高。同一款型写两遍尺寸不能让它的票数翻倍。"""
    brand = make_brand(db_session, "重复行品牌")
    series = make_series(db_session, brand, "重复行车")
    year = make_year(db_session, series)
    source = make_source(db_session)
    make_variant(
        db_session, series, year, config_version="A", price_cny=100000,
        facts=[("尺寸", F_SIZE, "4997*1963*1445", "mm", None)], source=source,
    )
    make_variant(
        db_session, series, year, config_version="B", price_cny=110000,
        facts=[("尺寸", F_SIZE, "4997*1963*1460", "mm", None)], source=source,
    )
    # A 款再补一条同值尺寸行：若按行计数，A 会变成 2:1 并压过 B
    make_variant(
        db_session, series, year, config_version="C", price_cny=120000,
        facts=[
            ("尺寸", F_SIZE, "4997*1963*1460", "mm", None),
            ("尺寸", F_SIZE, "4997*1963*1460", "mm", None),
        ],
        source=source,
    )
    text = size_line(db_session, series)
    assert text is not None
    assert "在售 3 款中 2 款为此尺寸" in text, text




# ── 3. RAG 车系摘要切片必须带 label ───────────────────────────────────────
def test_rag_series_slice_keeps_headline_labels(db_session: Session):
    """切片文本会作为「数据佐证」**直接进 LLM 上下文**。

    此前 `_summary_chunk_text` 里是 `[headline[label] for label in HEADLINE_ORDER]`
    ——只取值、丢掉 label，切片读起来是：

        核心参数：4135*1805*1570 mm；2650 mm；58~85 kW；310~480 km（CLTC）

    `2650 mm` 是轴距还是车长？`58~85 kW` 是电机还是系统功率？读者和 LLM 都分不出来。
    这是本轮发现的、比「缺一个全系极值标注」严重得多的问题。
    """
    brand = make_brand(db_session, "切片品牌")
    series = make_series(db_session, brand, "切片车")
    headline = {
        "尺寸": "4135*1805*1570 mm",
        "轴距": "2650 mm",
        "动力": "58~85 kW",
        "续航": "310~480 km（CLTC）",
        "电池": "30.12~47.14 kWh",
    }
    text = _summary_chunk_text(series, brand, headline, (64800, 94800), 6, None)
    # 切片各段用空格连接，所以切出来的片段带前导空格
    core = next(
        p.strip() for p in text.split("。") if p.strip().startswith("核心参数")
    )
    for label in headline:
        assert f"{label} {headline[label]}" in core, f"{label} 没带上：{core}"
    # 与卡片用同一个前缀常量（用户 2026-10-05 拍板）
    assert core.startswith(HEADLINE_PREFIX), core


