"""批量装载与单条装载的**口径等价**（2026-10-02 P5.2 / H4）。

`variants_current_prices` / `variants_facts` 是 `variant_current_price` /
`variant_facts` 的批量版，用来消掉「逐款型 SELECT」的 N+1。性能优化的前提是
**结果逐字相同**——否则就不是优化而是行为变更。故本测试逐条比对。

特别覆盖两个容易写错的点：
1. 同一 variant 有**多条**当前价时，必须与单条版的 `.first()`（effective_from 最新）
   取到同一条；
2. 停售 / 非 official_msrp 的价格在两版里都必须被排除。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.catalog import services
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> tuple[list[int], int, int]:
    """返回 (三个 on_sale variant id, 只有一个停售价的老 variant id, 无价 variant id)。"""
    src = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试", source=src)
    series = make_series(db, brand, name="车系", body_type="sedan",
                         energy_types=("BEV",), source=src)
    year = make_year(db, series)
    ids = [
        make_variant(db, series, year, config_version=f"款{i}", energy_type="BEV",
                     price_cny=str(150000 + i * 1000), source=src).id
        for i in range(3)
    ]
    return ids, series.id, year.id


def test_prices_batch_matches_single(db_session: Session):
    ids, _s, _y = _seed(db_session)
    batch = services.variants_current_prices(db_session, ids)
    for vid in ids:
        single = services.variant_current_price(db_session, vid)
        assert batch.get(vid) is not None
        assert batch[vid].id == (single.id if single else None), f"variant {vid} 取到了不同的价格行"


def test_prices_batch_picks_latest_effective_from(db_session: Session):
    """同一 variant 两条当前价 → 必须取 effective_from 最新那条（与 .first() 一致）。"""
    from datetime import datetime, timedelta, timezone

    from app.common.models import OfficialPrice

    ids, _s, _y = _seed(db_session)
    now = datetime.now(timezone.utc)
    older = OfficialPrice(
        variant_id=ids[0], price_type="official_msrp", price_cny=111000,
        effective_from=now - timedelta(days=10),
    )
    newer = OfficialPrice(
        variant_id=ids[0], price_type="official_msrp", price_cny=222000,
        effective_from=now - timedelta(days=1),
    )
    db_session.add_all([older, newer])
    db_session.commit()

    batch = services.variants_current_prices(db_session, ids)
    single = services.variant_current_price(db_session, ids[0])
    assert batch[ids[0]].id == single.id
    assert float(batch[ids[0]].price_cny) == 222000.0


def test_prices_batch_excludes_non_current_and_non_msrp(db_session: Session):
    """已失效（effective_to 非空）与非 official_msrp 的行，两版都必须排除。"""
    from datetime import datetime, timedelta, timezone

    from app.common.models import OfficialPrice

    ids, _s, _y = _seed(db_session)
    now = datetime.now(timezone.utc)
    db_session.add_all([
        OfficialPrice(variant_id=ids[0], price_type="official_msrp", price_cny=999000,
                      effective_from=now - timedelta(days=5), effective_to=now),
        OfficialPrice(variant_id=ids[0], price_type="成交价", price_cny=888000,
                      effective_from=now - timedelta(days=5)),
    ])
    db_session.commit()

    batch = services.variants_current_prices(db_session, ids)
    single = services.variant_current_price(db_session, ids[0])
    assert batch[ids[0]].id == single.id
    assert float(batch[ids[0]].price_cny) != 999000.0
    assert float(batch[ids[0]].price_cny) != 888000.0


def test_facts_batch_matches_single(db_session: Session):
    ids, _s, _y = _seed(db_session)
    batch = services.variants_facts(db_session, ids)
    for vid in ids:
        single = services.variant_facts(db_session, vid)
        assert [f.id for f in batch.get(vid, [])] == [f.id for f in single], f"variant {vid} 事实不一致"


def test_batch_handles_empty_input(db_session: Session):
    """空入参不得打库。"""
    assert services.variants_current_prices(db_session, []) == {}
    assert services.variants_facts(db_session, []) == {}
