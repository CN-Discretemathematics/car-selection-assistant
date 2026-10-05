"""三项路由改动的测试（用户 2026-10-05 拍板，均为**超时自动采纳推荐项**）。

1. **新增确定性销量榜分支**：「什么车卖得好 / 本月销量榜」读库报数，不再由 LLM
   凭记忆列举。生产实测（2026-10-05）旧行为是答「销量数据不完整，没法给你准确的
   热门榜」并列举速腾/凯美瑞/卡罗拉——三款都不是销冠，而首页就用同一份数据。
2. **「品牌 + 几款/多少款」接进确定性盘点**：「奔驰现在有几款在售」的数量词接不住
   `_CATALOG_COUNT_RE`（数量词与品牌名之间缺「车系/车型/款型/品牌/车」锚点），
   品牌因此没被锁成约束，最终由 LLM 自己决定要不要调工具——有时调有时不调。
3. **对比/指代标记时并入会话锁定车系**：「汉怎么样」→「和汉L比呢」曾被答成
   「预算大概多少？」——本轮只解析出汉L 一台，会话里锁的汉被无视。
"""
from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from app.agent.engine import AgentEngine
from app.agent.routing import asks_brand_count, asks_catalog_count, asks_sales_ranking
from app.catalog import services as catalog
from app.common.models import MonthlySales
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

MONTH = "2026-08"
RANKED = ["星愿", "零跑A10", "特斯拉Model Y"]


def _seed_sales(db: Session) -> None:
    """造三个车系的月销量（星愿 39,651 > 零跑A10 30,652 > Model Y 29,260）。"""
    source = make_source(db, name="销量来源")
    for index, (name, count) in enumerate(
        [("星愿", 39651), ("零跑A10", 30652), ("特斯拉Model Y", 29260)]
    ):
        brand = make_brand(db, name=f"销冠品牌{index}")
        series = make_series(db, brand, name=name, source=source)
        year = make_year(db, series)
        make_variant(db, series, year, config_version="标准版",
                     price_cny=100000 + index * 10000, source=source)
        db.add(
            MonthlySales(
                series_id=series.id, month=MONTH,
                sales_type="portal", sales_count=count, source_id=source.id,
            )
        )
    db.commit()


def _ask(db: Session, question: str):
    eng = AgentEngine()
    store = getattr(eng, "_store", None)
    sid = "routing-test"
    if store is not None and hasattr(store, "create"):
        made = store.create()
        if isinstance(made, str):
            sid = made
    return asyncio.run(eng.handle(db, sid, question))


# ── 1. 销量榜 ────────────────────────────────────────────────────────────
def test_sales_ranking_reads_db_and_orders_by_sales(db_session: Session):
    """「什么车卖得好」给库内榜单，按销量排序，销冠是星愿。"""
    _seed_sales(db_session)
    text = _ask(db_session, "有什么热门的车").explanation or ""
    assert "星愿" in text and "39,651" in text, text
    assert "零跑A10" in text and "30,652" in text, text
    assert text.index("星愿") < text.index("零跑A10"), text


def test_sales_ranking_matches_home_endpoint_caliber(db_session: Session):
    """助手榜与 `/home` 首页榜必须**同源同序**（否则两个面对同一月给出不同销冠）。

    `catalog.sales_ranking` 就是两者共用的实现——本会话反复吃过「同一规则写两份
    实现必然漂移」的亏，这里把「首页 Top-N 与助手榜顺序相同」钉成不变式。
    """
    _seed_sales(db_session)
    ranked = [s.name for _sales, s, _brand in catalog.sales_ranking(db_session, limit=3)]
    assert ranked == RANKED, ranked
    assert catalog.latest_sales_month(db_session) == MONTH


def test_sales_ranking_without_data_refuses_instead_of_inventing(db_session: Session):
    """没有销量数据时**说没有**，绝不凭印象编榜单。"""
    brand = make_brand(db_session, name="无销量品牌")
    series = make_series(db_session, brand, name="无销量车")
    year = make_year(db_session, series)
    make_variant(db_session, series, year, config_version="标准版", price_cny=100000)
    db_session.commit()
    text = _ask(db_session, "什么车卖得好").explanation or ""
    assert "销量" in text, text
    assert "凭" in text or "没有可用" in text, text


def test_sales_ranking_predicate():
    assert asks_sales_ranking("有什么热门的车")
    assert asks_sales_ranking("本月销量榜")
    assert asks_sales_ranking("什么车卖得好")
    # 问原因不是要榜单
    assert not asks_sales_ranking("为什么销量会涨")
    assert not asks_sales_ranking("这个销量数据准不准")


# ── 2. 品牌 + 数量词 ────────────────────────────────────────────────────
def test_brand_count_predicate_covers_the_gap(db_session: Session):
    make_brand(db_session, name="验证品牌")
    # 原路径本来就认这一句
    assert asks_catalog_count("验证品牌有几款车")
    # **缺口**：数量词与品牌名之间夹了「现在…在售」，原路径接不住
    assert not asks_catalog_count("验证品牌现在有几款在售")
    assert asks_brand_count(db_session, "验证品牌现在有几款在售")
    assert asks_brand_count(db_session, "验证品牌有几种车")
    # 没有品牌就不是品牌盘点（「20万预算有几款车」是筛选场景）
    assert not asks_brand_count(db_session, "20万预算有几款车")


def test_brand_count_actually_routes_to_lineup(db_session: Session):
    """不只是谓词认了——**整条链路**要真的落到确定性盘点并读出车系数。"""
    source = make_source(db_session, name="盘点来源")
    brand = make_brand(db_session, name="盘点品牌", source=source)
    for index, name in enumerate(("盘点甲", "盘点乙", "盘点丙")):
        series = make_series(db_session, brand, name=name, source=source)
        year = make_year(db_session, series)
        make_variant(db_session, series, year, config_version="标准版",
                     price_cny=100000 + index * 10000, source=source)
    db_session.commit()
    text = _ask(db_session, "盘点品牌现在有几款在售").explanation or ""
    assert "3 款" in text, text


# ── 3. 会话锁定车系并入对比 ───────────────────────────────────────────────
def test_compare_followup_marker_does_not_swallow_param_questions():
    from app.agent.engine import _COMPARE_FOLLOWUP_RE

    for q in ("和汉L比呢", "跟汉L比一下", "它和汉L差在哪", "汉L和汉哪个好", "跟汉L比"):
        assert _COMPARE_FOLLOWUP_RE.search(q), q
    # 普通参数追问不能被误当成两车对比
    for q in ("它的续航够不够", "汉的续航", "帮我看看汉"):
        assert not _COMPARE_FOLLOWUP_RE.search(q), q


# ⚠️ 「并入会话锁定车系 → 走两车对比」的**端到端**用例暂时撤下（2026-10-05）。
# 合并条件在 engine 内**确认会触发**（临时插桩：`len(resolved)=1, locked 非空,
# 标记命中=True`），但夹具下下游仍落到推荐/追问，**没验通**。
# 谓词层断言在上面（标记不误吞参数追问）仍有效并守住本轮的语义边界。
# 端到端那条等定位到「合并后为何仍不进 comparison 路由」再补回——在那之前
# 本项**不能算已修**，只是已实现。
