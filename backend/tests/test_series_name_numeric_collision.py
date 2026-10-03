"""车系解析：纯数字短名不得命中中文购车语里的预算/年份（2026-10-04）。

## 生产事故

用户输入「我最看重后排空间，预算20万要家用SUV」，系统没有出推荐卡片，而是回复
「关于『领克20』… 官方资料未披露」——**整条推荐链被跳过**，用户只看到一台没数据的车。

## 根因

`resolve_series` 的候选含「去掉品牌前缀的车系名」，于是：

    领克20 → 剥掉「领克」→ "20" → 子串匹配命中「预算**20**万」

`decide_route` 看到 `resolved_count=1` 就判 `series_qa`，推荐分支根本不执行。

**全库 26 个车系中招**：领克20/领克10/标致408/坦克500/极氪007/睿蓝7/阿维塔06 …
而中文购车语里裸数字几乎总是预算或年份。实测预算 6/7/8/9/10/11/12/20 万都会命中。

## 为什么砍掉纯数字候选不会伤真实用法

中文没有词边界，靠子串匹配分不开「20万」和「领克20」；但用户真要问某台车时
**必然带品牌或完整名**（「领克20」「坦克500」都仍是候选）。砍掉纯数字短名
消掉整类误伤，真实用法一个不丢。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.engine import extract_hints
from app.agent.routing import decide_route
from app.agent.schemas import UserProfile
from app.catalog.series_index import resolve_series
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> None:
    """造三台「剥掉品牌后只剩纯数字」的车系 + 一台普通车系。

    ⚠️ 品牌名**必须是车系名的真前缀**（生产数据如此：`领克` / `领克20`）。
    第一版我图省事写成 brand="测试牌"、name="测试牌领克20"，剥出来是「领克20」而非
    「20」——**根本没造出碰撞**，于是把 `not norm.isdigit()` 去掉测试照样全绿。
    是反向验证抓出来的。这类「种子没造出问题」比没测试更危险。
    """
    source = make_source(db, name="汽车之家")
    plan = [
        # (品牌名, 车系名, 车身, 能源)  —— 剥品牌后分别是 '20' / '500' / '007'
        ("领克", "领克20", "suv", "BEV"),
        ("坦克", "坦克500", "suv", "BEV"),
        ("极氪", "极氪007", "sedan", "BEV"),
        ("日产", "轩逸", "sedan", "ICE"),
    ]
    for brand_name, name, body, energy in plan:
        brand = make_brand(db, name=brand_name, source=source)
        s = make_series(db, brand, name=name, body_type=body, energy_types=(energy,), source=source)
        y = make_year(db, s)
        make_variant(db, s, y, price_cny="180000", energy_type=energy, source=source)
    db.commit()


@pytest.mark.parametrize(
    "message",
    [
        "预算20万",
        "20万预算",
        "预算20万要家用SUV",
        "我最看重后排空间，预算20万要家用SUV",   # ← 线上出事的那句
        "预算10万以内",
        "20万买什么车",
        "2026款多少钱",
        "预算50万",
        "预算30万想买台车",
    ],
)
def test_numeric_budget_is_not_read_as_a_series(db_session: Session, message):
    """预算/年份里的裸数字**不得**被认成车系。"""
    _seed(db_session)
    got = [s.name for s, _ in resolve_series(db_session, message)]
    assert got == [], f"「{message}」被误识别成车系 {got}"


@pytest.mark.parametrize(
    "message",
    [
        "领克20怎么样",
        "坦克500的油耗",
        "极氪007多少钱",
        "看看领克20",
    ],
)
def test_real_brand_prefixed_usage_still_resolves(db_session: Session, message):
    """带品牌的真实用法**必须**照常识别——这是砍纯数字候选的代价红线。"""
    _seed(db_session)
    got = [s.name for s, _ in resolve_series(db_session, message)]
    assert got, f"「{message}」本该识别到车系，却一个都没匹配"


def test_budget_message_routes_to_recommendation_not_series_qa(db_session: Session):
    """端到端红线：出事那句话必须判 `recommendation`，否则推荐链整条被跳过。"""
    _seed(db_session)
    message = "我最看重后排空间，预算20万要家用SUV"
    hints = extract_hints(message)
    resolved = resolve_series(db_session, message)
    d = decide_route(message, hints, bool(hints), UserProfile(), resolved, db_session)
    assert d.intent == "recommendation", (
        f"被判成 {d.intent}（signals={d.signals}）——推荐链会被跳过"
    )


def test_hints_still_parsed_when_series_is_not_resolved(db_session: Session):
    """车系没识别出来时，**硬约束与强调词必须照常抽到**，不能连累。"""
    _seed(db_session)
    hints = extract_hints("我最看重后排空间，预算20万要家用SUV")
    assert hints.get("body_type") == ["suv"]
    assert hints.get("budget", {}).get("max") == 200000.0
    assert "家庭" in (hints.get("usage") or [])
    assert hints.get("weights", {}).get("space") is not None, "强调词权重丢了"


def test_brand_and_series_together_still_resolves(db_session: Session):
    """「品牌+车系」组合候选不受影响。"""
    _seed(db_session)
    got = [s.name for s, _ in resolve_series(db_session, "领克20怎么样")]
    assert got == ["领克20"]
