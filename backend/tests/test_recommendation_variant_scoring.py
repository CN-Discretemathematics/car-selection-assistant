# -*- coding: utf-8 -*-
"""同车系**不同款型**必须能区分开（2026-10-24 生产实测缺陷）

## 缺陷（真人实测，不是构造）

2026-10-24 22:54 生产实测（session `63301638…`，用户是真人）：

> 「10万块燃油车给我推荐5款」⇒ 推荐里出现**长安逸动**的三个款型：
> 劲享型 8.79万 / 悦享型 9.39万 / 智享型 9.99万，
> **三张卡片的匹配分完全相同（0.8816）**。

逐维实测（生产库，`include_dims=True`）：

| 维度 | 劲享型 | 悦享型 | 智享型 |
| --- | --- | --- | --- |
| budget | 1.0000 | 1.0000 | 1.0000（都在预算内 ⇒ 恒为满分） |
| usage / energy | 1.0000 | 1.0000 | 1.0000（**车系级**：同为轿车 + 燃油） |
| space | 0.5389 | 0.5389 | 0.5389（**车系级**：车长相同） |
| power | 0.2773 | 0.2773 | 0.2773（**车系级**：141kW/192马力/310N·m 相同） |
| comfort / intelligence | 1.0000 | 1.0000 | 1.0000（**布尔**：有该类事实即满分） |
| maintenance | 1.0000 | 1.0000 | 1.0000（**车系级**：按品牌规模+销量活跃度算） |

⇒ **8 个维度逐位相同** ⇒ 三个款型分数必然相同。

## 根因

打分维度全是**车系级**的（功率/车长/能源）或**布尔**的（舒适/智能「有/无该类事实」）
⇒ 同车系的款型之间**没有任何一个维度能区分它们**。

而库里其实**有**区分依据（实测）：

| 款型 | spec_facts 总条数 | 标配（●）项数 |
| --- | --- | --- |
| 劲享型 8.79万 | 97 | 30 |
| 悦享型 9.39万 | 101 | 32 |
| 智享型 9.99万 | 115 | **39** |

## 本文件的判据

- 同车系不同款型 ⇒ **分数必须能区分**（配置多的款型分更高）；
- 区分依据是**标配（●）项数**，不是「选装/没有」——数选装等于编造配置更好；
- 绝对项数**跨车系不可比**（紧凑级天然配置少）⇒ 只在**本批候选内**归一化；
- 不回归：款型之间**配置完全相同**时，分数仍然相同（不能凭噪声分出高下）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.schemas import Budget, UserProfile
from app.agent.tools import recommendation_tool
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_same_series_variants(db: Session) -> dict[str, int]:
    """同一车系、三个款型：**只有配置项数不同**，其余事实完全相同。

    复刻生产实况：逸动 劲享型/悦享型/智享型（141kW、2765mm 轴距、同一车系）。
    """
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="长安", source=source)
    series = make_series(
        db, brand, name="逸动", body_type="sedan", energy_types=("ICE",), source=source
    )
    year = make_year(db, series)

    ids: dict[str, int] = {}
    for config, price, n_std in (("劲享型", "87900", 30), ("悦享型", "93900", 32), ("智享型", "99900", 39)):
        facts = [
            # 车系级事实：三个款型**完全相同**（同款车不同配置，动力/尺寸一致）
            ("参数信息", "最大功率(kW)", "141", "kW", None),
            ("参数信息", "发动机最大功率(kW)", "141", "kW", None),
            ("基本参数", "长*宽*高(mm)", "4650*1800*1475", "mm", None),
            ("基本参数", "座位数(个)", "5", "个", None),
        ]
        # 配置类事实：只数**标配（●）**，数量随款型递增。
        # 类别用**生产真实类别**（内部配置 / 安全配置 / 智能辅助驾驶）——
        # 「舒适性」这个键在生产数据里根本不存在。
        for i in range(n_std // 2):
            facts.append(("内部配置", f"内部项{i}", "●", None, None))
        for i in range(n_std // 3):
            facts.append(("安全配置", f"安全项{i}", "●", None, None))
        for i in range(n_std - n_std // 2 - n_std // 3):
            facts.append(("智能/辅助驾驶", f"智能项{i}", "●", None, None))
        variant = make_variant(
            db, series, year, config_version=config, energy_type="ICE",
            price_cny=price, facts=facts, source=source,
        )
        ids[config] = variant.id
    db.commit()
    return ids


def _profile() -> UserProfile:
    p = UserProfile()
    p.budget = Budget(max=100000.0, currency="CNY", type="official_msrp")
    p.usage = ["通勤"]
    p.energy_preference = ["ICE"]
    return p


def _scores(db: Session, ids: dict[str, int]) -> dict[str, float]:
    result = recommendation_tool(db, _profile(), limit=10)
    out = {}
    for v in result["variants"]:
        for config, vid in ids.items():
            if v["variant_id"] == vid:
                out[config] = v["score"]
    return out


def test_same_series_variants_get_different_scores(db_session: Session):
    """**核心判据**：同一车系的三个款型 ⇒ 分数必须能区分。

    生产实测：逸动 劲享型/悦享型/智享型 三张卡的匹配分**完全相同**（0.8816）。
    """
    ids = _seed_same_series_variants(db_session)
    scores = _scores(db_session, ids)

    assert len(scores) == 3, f"三个款型都该进候选：{scores}"
    assert len(set(scores.values())) > 1, (
        f"同车系不同款型的分数**必须能区分**，实测全相同：{scores}"
    )
    # 配置越多的款型分越高（标配项数 30 < 32 < 39）
    assert scores["智享型"] > scores["悦享型"] > scores["劲享型"], (
        f"标配项多的款型分应更高（39 > 32 > 30）：{scores}"
    )


def test_same_series_identical_config_still_ties(db_session: Session):
    """**不回归**：款型之间**配置完全相同**时，分数仍然相同。

    不能凭噪声把配置一模一样的款型分出高下——那是编造差异。
    """
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="长安", source=source)
    series = make_series(
        db_session, brand, name="逸动", body_type="sedan", energy_types=("ICE",), source=source
    )
    year = make_year(db_session, series)
    ids = {}
    for config in ("标准A", "标准B"):
        facts = [
            ("参数信息", "最大功率(kW)", "141", "kW", None),
            ("基本参数", "长*宽*高(mm)", "4650*1800*1475", "mm", None),
        ]
        for i in range(20):
            facts.append(("安全配置", f"配置项{i}", "●", None, None))
        v = make_variant(
            db_session, series, year, config_version=config, energy_type="ICE",
            price_cny="87900", facts=facts, source=source,
        )
        ids[config] = v.id
    db_session.commit()

    scores = _scores(db_session, ids)
    assert len(scores) == 2, scores
    assert scores["标准A"] == scores["标准B"], (
        f"配置完全相同的款型分数应当相同（不能编造差异）：{scores}"
    )


def test_selection_markers_not_counted_as_standard(db_session: Session):
    """**选装/没有不算标配**——数进去等于谎称它配置更好。

    生产配置表里 `-`/`—`/`○` 表示选装或没有，只有 `●` 才是标配。
    """
    from app.agent.tools import _count_standard_items

    facts = [
        {"category": "安全配置", "value": "●"},
        {"category": "安全配置", "value": "-"},
        {"category": "安全配置", "value": "—"},
        {"category": "安全配置", "value": "○"},
        {"category": "安全配置", "value": "前● / 后●"},
        {"category": "内部配置", "value": "●"},
    ]
    # comfort 类别内：'●' + '前● / 后●'（复合形态也算）+ 内部配置的 '●' = 3
    # ⚠️ '-' / '—' / '○'（没有 / 选装）**不算**——数进去等于谎称它配置更好。
    # ⚠️ 判定是「值里含 '●'」而非「值 == '●'」——生产里 34.3 万条是 '●'，
    #    另有 2.1 万条 '前● / 后●' 这类复合形态（见 _STANDARD_MARK 的说明）。
    assert _count_standard_items(facts, ("舒适性", "内部配置", "外部配置", "安全配置", "操控配置")) == 3
    assert _count_standard_items(facts, ("智能驾驶", "智能座舱", "智能/辅助驾驶")) == 0


def test_safety_config_category_is_counted(db_session: Session):
    """**安全配置必须参与打分**——它是生产库第二大配置类事实（10.5 万条）。

    2026-10-24 生产实测的根因：`_COMFORT_CATEGORIES` 漏了「安全配置」
    ⇒ 安全配置差异**完全不影响分数** ⇒ 同车系配置明显不同的款型得分相同
    （逸动 劲享型 8 项 / 悦享型 10 项 / 智享型 13 项标配，分数却全一样）。
    """
    from app.agent.tools import _COMFORT_CATEGORIES

    assert "安全配置" in _COMFORT_CATEGORIES, (
        "「安全配置」必须进 comfort 维度——它是生产第二大配置类事实，漏了它"
        "就等于「安全配置差异不影响推荐」"
    )
    assert "操控配置" in _COMFORT_CATEGORIES, "「操控配置」同理（生产 2.6 万条）"


def test_private_fields_not_in_production_payload(db_session: Session):
    """私有字段（原始项数）**不得**进生产响应体。"""
    ids = _seed_same_series_variants(db_session)
    result = recommendation_tool(db_session, _profile(), limit=10)
    for v in result["variants"]:
        assert "_comfort_items" not in v, v
        assert "_intelligence_items" not in v, v
        assert "_dims" not in v, v
        assert "_measured" not in v, v
    assert len(ids) == 3
