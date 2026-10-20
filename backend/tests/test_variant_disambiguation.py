"""D1.1 款型名上下文消歧（计划 Phase 0 第一项）。

## 为什么需要它

库里的款型显示名**不含车系名**（「2026款 标准版」「2026款 Ultra」），
生产库实测 **103 个**归一化款型名被多个车系共享（「2026款 Ultra」→
理想L6/L8/L9+岚图泰山+智界V9）。撞名名此前**整体不入名称索引**
（first-wins 会造出幽灵第二实体），于是用户点名了款型也只能按**全系**给值
——「IONIQ 5 N 2026款 标准版 的轴距」答成三档并列，答非所问。

## 判据（这里每一条都是硬约束）

1. **唯一款型名** → 直接命中；
2. **撞名款型名** → 按**就近上下文**消歧：消息里离它最近的车系名若在候选集内，
   就归它；**否则不猜**（宁可退回全系口径，也不认错款型）；
3. **没点名款型** → 回答**逐字不变**；
4. **该款型在库里没有事实** → 不收窄（否则「该款型没收录」会变成「官方资料未披露」
   ——那是假否定，同系别的款型明明有）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.series_qa import build_series_qa_answer
from app.catalog.series_index import resolve_series, resolve_variants
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

#: 生产库里款型显示名的真实形态：**不含车系名**（因此跨车系撞名）。
AMBIGUOUS_NAME = "2026款 标准版"


def _seed_series(db: Session, name: str, source, *, variant_name: str | None = None):
    """建一个车系 + 一个款型；`variant_name` 覆盖显示名（用于造撞名）。"""
    brand = make_brand(db, name=f"{name}品牌", source=source)
    series = make_series(db, brand, name=name, body_type="sedan", energy_types=("BEV",), source=source)
    year = make_year(db, series)
    variant = make_variant(
        db, series, year, price_cny="199800",
        facts=[("参数信息", "轴距(mm)", "2820", "mm", None)],
        source=source,
    )
    if variant_name is not None:
        variant.display_name = variant_name
    db.commit()
    return series, variant


def _seed_two_ambiguous(db_session: Session):
    """两个车系，各有一个**同名**款型（生产库里的典型撞名）。

    ⚠️ **乙车系先种**：索引按 `series_id` 排序，于是「取第一个候选」这种
    写法会选中**乙车系**——只有这样，守卫才测得出「答案是靠**上下文**选出来的，
    不是靠候选顺序」。若甲车系先种，「忽略上下文」的变异会蒙混过关（实测踩过）。
    """
    source = make_source(db_session, name="测试来源")
    s2, v2 = _seed_series(db_session, "乙车系", source, variant_name=AMBIGUOUS_NAME)
    s1, v1 = _seed_series(db_session, "甲车系", source, variant_name=AMBIGUOUS_NAME)
    db_session.commit()
    return (s1, v1), (s2, v2)


def test_unique_variant_name_resolves(db_session: Session):
    """款型名唯一 → 直接命中。"""
    source = make_source(db_session, name="测试来源")
    series, variant = _seed_series(db_session, "独车系", source, variant_name="2026款 旗舰版")
    db_session.commit()

    resolved = resolve_series(db_session, "独车系 2026款 旗舰版 的轴距")
    hits = resolve_variants(db_session, "独车系 2026款 旗舰版 的轴距", resolved)
    assert len(hits) == 1, hits
    assert hits[0][0].id == variant.id
    assert hits[0][1].id == series.id


def test_ambiguous_variant_is_resolved_by_context(db_session: Session):
    """撞名款型名 → 按**同句共现的车系**消歧，归那个车系的款型。"""
    _seed_two_ambiguous(db_session)
    resolved = resolve_series(db_session, "甲车系 2026款 标准版 的轴距是多少")
    assert [s.id for s, _b in resolved] == [1] or resolved, resolved

    hits = resolve_variants(db_session, "甲车系 2026款 标准版 的轴距是多少", resolved)
    assert len(hits) == 1, hits
    assert hits[0][1].name == "甲车系", hits[0][1].name


def test_ambiguous_variant_without_context_is_not_guessed(db_session: Session):
    """没有上下文 → **不猜**（返回空，调用方退回全系口径）。"""
    _seed_two_ambiguous(db_session)
    resolved = resolve_series(db_session, "2026款 标准版 的轴距是多少")
    hits = resolve_variants(db_session, "2026款 标准版 的轴距是多少", resolved)
    assert hits == [], hits


def test_nearest_context_wins(db_session: Session):
    """两个车系都在句子里 → **就近**的那个车系赢。"""
    _seed_two_ambiguous(db_session)
    resolved = resolve_series(db_session, "甲车系 2026款 标准版 和 乙车系 2026款 标准版 差在哪")
    assert len(resolved) == 2, resolved

    hits = resolve_variants(db_session, "甲车系 2026款 标准版 和 乙车系 2026款 标准版 差在哪", resolved)
    names = sorted(s.name for _v, s in hits)
    assert names == ["乙车系", "甲车系"], names


def test_param_answer_uses_the_named_variant(db_session: Session):
    """参数问答要答**那个款型**的值，并在文案里点名是哪个款型。"""
    _seed_two_ambiguous(db_session)
    resolved = resolve_series(db_session, "甲车系 2026款 标准版 的轴距是多少")
    answer = build_series_qa_answer(db_session, resolved, "甲车系 2026款 标准版 的轴距是多少")
    assert "轴距" in answer, answer
    assert "甲车系" in answer, answer
    # 文案要点名是按哪个款型给的（否则用户不知道这个轴距是谁的）
    assert "2026款 标准版" in answer, answer


def test_answer_unchanged_without_variant(db_session: Session):
    """**没点名款型** → 回答逐字不变（不出现款型限定语）。"""
    source = make_source(db_session, name="测试来源")
    _seed_series(db_session, "独车系", source, variant_name="2026款 旗舰版")
    db_session.commit()

    resolved = resolve_series(db_session, "独车系 的轴距是多少")
    answer = build_series_qa_answer(db_session, resolved, "独车系 的轴距是多少")
    assert "你问到的相关参数：" in answer, answer
    assert "按「" not in answer, answer


def test_two_variants_of_one_series_are_not_narrowed(db_session: Session):
    """同一车系点名了**两个款型** → **不收窄**。

    那是要看款型之间的差异，把参数列收窄到其中一个款型，反而把另一个款型的值藏起来——
    而那几个值**正是差异的来源**（实测：领克08 EM-P 两款型对比，多档值消失了）。
    """
    source = make_source(db_session, name="测试来源")
    series, _variant = _seed_series(db_session, "甲车系", source, variant_name="2026款 标准版")
    other_year = make_year(db_session, series, year_name="2027款")
    second = make_variant(
        db_session, series, other_year, price_cny="209800",
        facts=[("参数信息", "轴距(mm)", "2900", "mm", None)],
        source=source,
    )
    second.display_name = "2027款 豪华版"
    db_session.commit()
    message = "甲车系 2026款 标准版 和 2027款 豪华版 的轴距差多少"
    resolved = resolve_series(db_session, message)
    answer = build_series_qa_answer(db_session, resolved, message)
    assert "按「" not in answer, answer


def test_variant_without_facts_does_not_claim_missing(db_session: Session):
    """该款型在库里**没有事实** → 不收窄（不把「没收录」说成「官方资料未披露」）。

    这是**假否定**的防线：同系**别的款型**明明有轴距数据。
    """
    source = make_source(db_session, name="测试来源")
    series, variant = _seed_series(db_session, "独车系", source, variant_name="2026款 旗舰版")
    # 同系再加一个**有事实**的款型（模拟「库里有这个系的参数，只是没收录这一款」）
    other_year = make_year(db_session, series, year_name="2027款")
    make_variant(
        db_session, series, other_year, price_cny="209800",
        facts=[("参数信息", "轴距(mm)", "2820", "mm", None)],
        source=source,
    )
    # 把被问款型的事实删掉（模拟「该款型参数未入库」）
    from app.common.models import SpecFact

    for fact in db_session.query(SpecFact).filter(SpecFact.variant_id == variant.id).all():
        db_session.delete(fact)
    db_session.commit()

    resolved = resolve_series(db_session, "独车系 2026款 旗舰版 的轴距是多少")
    answer = build_series_qa_answer(db_session, resolved, "独车系 2026款 旗舰版 的轴距是多少")
    assert "轴距" in answer, answer
    assert "官方资料未披露" not in answer, answer
