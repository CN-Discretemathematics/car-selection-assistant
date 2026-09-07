"""能源标注复算工具测试（tools/fix_energy_labels.py）。

复刻线上问题：插混 SKU 被标成 ICE（用户说「想买15万的燃油车」却被推插混），
验证工具用生产判定函数复算后：只改错标行、不动正确行、同步车系能源并集、
dry-run 不写库、修复后复跑幂等。
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fix_energy_labels import repair_energy_labels  # noqa: E402

from app.common.models import VehicleSeries, VehicleVariant  # noqa: E402
from tests.seed import (  # noqa: E402
    make_brand,
    make_series,
    make_source,
    make_variant,
    make_year,
)


def _seed_labels(db: Session) -> dict[str, int]:
    """两个错标插混 + 一个正确纯电 + 一个正确燃油。"""
    source = make_source(db, name="汽车之家")
    brand_ge = make_brand(db, name="银河", source=source)
    brand_tc = make_brand(db, name="丰田", source=source)

    # 车系并集也是错的（入库时由款型标注反推）：只有 ICE
    xingyao8 = make_series(db, brand_ge, name="星耀8", body_type="sedan",
                           energy_types=("ICE",), source=source)
    star = make_series(db, brand_ge, name="星愿", body_type="sedan",
                       energy_types=("BEV",), source=source)
    corolla = make_series(db, brand_tc, name="卡罗拉", body_type="sedan",
                          energy_types=("ICE",), source=source)

    year_p = make_year(db, xingyao8)
    named = make_variant(
        db, xingyao8, year_p, config_version="225km EM-i 远航家航海版",
        powertrain="1.5T", energy_type="ICE", price_cny="142800", source=source,
        facts=[
            ("参数信息", "CLTC纯电续航里程(km)", "225", "km", "CLTC"),
            ("参数信息", "电池能量(kWh)", "18.4", "kWh", None),
            ("参数信息", "发动机最大功率(kW)", "120", "kW", None),
            ("参数信息", "排量(L)", "1.5", "L", None),
        ],
    )
    # 款型名没有任何能源标识词：只能靠电驱证据判定
    evidence = make_variant(
        db, xingyao8, year_p, config_version="220km 四驱远航版",
        powertrain="1.5T", energy_type="ICE", price_cny="116800", source=source,
        facts=[
            ("参数信息", "CLTC纯电续航里程(km)", "220", "km", "CLTC"),
            ("参数信息", "电池能量(kWh)", "18.1", "kWh", None),
            ("参数信息", "发动机最大功率(kW)", "120", "kW", None),
        ],
    )

    year_b = make_year(db, star)
    bev = make_variant(
        db, star, year_b, config_version="310km 向往版",
        powertrain="纯电动", energy_type="BEV", price_cny="64800", source=source,
        facts=[
            ("参数信息", "CLTC综合续航(km)", "310", "km", "CLTC"),
            ("参数信息", "电池能量(kWh)", "30.12", "kWh", None),
            ("参数信息", "电动机总功率(kW)", "58", "kW", None),
            # 评审 m17：纯电款型常存「排量(L)=新能源」这类非数值行，
            # 不得被当成「有发动机」证据（否则叠加电池能量会被判成油混/插混）
            ("参数信息", "排量(L)", "新能源", "L", None),
        ],
    )

    year_i = make_year(db, corolla)
    ice = make_variant(
        db, corolla, year_i, config_version="1.5L 精英版",
        powertrain="1.5L", energy_type="ICE", price_cny="128800", source=source,
        facts=[
            ("参数信息", "排量(L)", "1.5", "L", None),
            ("参数信息", "发动机最大功率(kW)", "88", "kW", None),
            # 启动电池按 Ah 标注：不得被当成「有动力电池 → 油混」
            ("参数信息", "电池容量(Ah)", "60", "Ah", None),
        ],
    )
    db.commit()
    return {
        "named": named.id,
        "evidence": evidence.id,
        "bev": bev.id,
        "ice": ice.id,
        "xingyao8": xingyao8.id,
        "star": star.id,
        "corolla": corolla.id,
    }


def test_dry_run_reports_without_writing(db_session: Session):
    ids = _seed_labels(db_session)
    report = repair_energy_labels(db_session, apply=False)

    assert report["checked"] == 4
    changes = {c["variant_id"]: c for c in report["changes"]}
    assert changes[ids["named"]]["new"] == "PHEV", "款型名含 EM-i 应判插混"
    assert changes[ids["evidence"]]["new"] == "PHEV", "纯电续航 220km 应判插混"
    assert changes[ids["named"]]["old"] == "ICE"
    assert ids["bev"] not in changes, "纯电标注正确，不应改动"
    assert ids["ice"] not in changes, "真燃油（Ah 启动电池）不应误判成油混"
    assert dict(report["by_transition"]) == {"ICE→PHEV": 2}

    # dry-run 不得写库，但要报告车系并集应有的变化
    db_session.expire_all()
    assert db_session.get(VehicleVariant, ids["named"]).energy_type == "ICE"
    updates = {u["series_id"]: u for u in report["series_updates"]}
    assert updates[ids["xingyao8"]]["new"] == ["PHEV"]
    assert ids["star"] not in updates and ids["corolla"] not in updates


def test_apply_repairs_labels_and_series_union(db_session: Session):
    ids = _seed_labels(db_session)
    report = repair_energy_labels(db_session, apply=True)
    assert len(report["changes"]) == 2

    db_session.expire_all()
    named = db_session.get(VehicleVariant, ids["named"])
    assert named.energy_type == "PHEV"
    assert named.powertrain == "插电混动", "动力形式文案要跟着标注一起更正"
    assert db_session.get(VehicleVariant, ids["evidence"]).energy_type == "PHEV"
    assert db_session.get(VehicleVariant, ids["bev"]).energy_type == "BEV"
    assert db_session.get(VehicleVariant, ids["ice"]).energy_type == "ICE"
    assert db_session.get(VehicleSeries, ids["xingyao8"]).energy_types == ["PHEV"]
    assert db_session.get(VehicleSeries, ids["corolla"]).energy_types == ["ICE"]

    # 幂等：修复后复跑不再有变更
    again = repair_energy_labels(db_session, apply=False)
    assert again["changes"] == []
    assert again["series_updates"] == []


def test_series_filter_and_on_sale_only(db_session: Session):
    ids = _seed_labels(db_session)
    filtered = repair_energy_labels(db_session, series_filter="星耀", apply=False)
    assert filtered["checked"] == 2
    assert {c["variant_id"] for c in filtered["changes"]} == {ids["named"], ids["evidence"]}

    db_session.get(VehicleVariant, ids["named"]).status = "off_sale"
    db_session.commit()
    on_sale = repair_energy_labels(db_session, on_sale_only=True, apply=False)
    assert ids["named"] not in {c["variant_id"] for c in on_sale["changes"]}
    assert ids["evidence"] in {c["variant_id"] for c in on_sale["changes"]}


def test_on_sale_only_does_not_narrow_series_union(db_session: Session):
    """评审 M3：过滤跑（--on-sale-only）不得据不完整样本改写车系能源并集。

    停售 ICE + 在售 PHEV 的车系，若按在售样本重算并集会被收窄成 ["PHEV"]，
    丢掉停售款型的能源类型，偏离导入口径（并集覆盖全部款型）。
    """
    ids = _seed_labels(db_session)
    db_session.get(VehicleVariant, ids["named"]).status = "off_sale"
    db_session.commit()

    report = repair_energy_labels(db_session, on_sale_only=True, apply=True)
    assert report["series_union_skipped"] is True
    assert report["series_updates"] == []
    assert {c["variant_id"] for c in report["changes"]} == {ids["evidence"]}

    db_session.expire_all()
    # 款型标注照常修复，但车系并集保持原值（等全量跑再同步）
    assert db_session.get(VehicleVariant, ids["evidence"]).energy_type == "PHEV"
    assert db_session.get(VehicleSeries, ids["xingyao8"]).energy_types == ["ICE"]

    # 全量跑才同步并集：此时停售款型也参与复算，并集为两款型复算后的真实并集
    full = repair_energy_labels(db_session, apply=True)
    assert full["series_union_skipped"] is False
    db_session.expire_all()
    assert db_session.get(VehicleSeries, ids["xingyao8"]).energy_types == ["PHEV"]
    assert db_session.get(VehicleVariant, ids["named"]).energy_type == "PHEV"


def test_unique_key_conflict_is_skipped_and_reported(db_session: Session):
    """唯一键安全阀：业务键含 powertrain，改写会撞键的行必须跳过并报告，绝不硬写。

    实测场景（AION V）：库内存在同业务键重复款型，靠 powertrain 不同区分
    （一行「插电混动」标注正确、一行「油电混动」误标 HEV）；把后者改成 PHEV
    会与前者撞 uq_variant_business_key，整个事务回滚。
    """
    ids = _seed_labels(db_session)
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="埃安", source=source)
    aion = make_series(db_session, brand, name="埃安V", body_type="suv",
                       energy_types=("PHEV", "HEV"), source=source)
    year = make_year(db_session, aion)
    good = make_variant(
        db_session, aion, year, config_version="霸王龙 插电混动",
        powertrain="插电混动", energy_type="PHEV", price_cny="159900", source=source,
        facts=[("参数信息", "CLTC纯电续航里程(km)", "210", "km", "CLTC")],
    )
    # 同键重复行：名字同样带「插电」→ 复算为 PHEV，写库会与 good 撞键
    dup = make_variant(
        db_session, aion, year, config_version="霸王龙 插电混动",
        powertrain="油电混动", energy_type="HEV", price_cny="159900", source=source,
        facts=[("参数信息", "CLTC纯电续航里程(km)", "210", "km", "CLTC")],
    )
    db_session.commit()

    report = repair_energy_labels(db_session, apply=True)
    assert [c["variant_id"] for c in report["skipped_conflicts"]] == [dup.id]
    # 冲突行也在 changes 里，但带 conflict 标记
    flagged = {c["variant_id"]: c["conflict"] for c in report["changes"]}
    assert flagged[dup.id] is True and flagged[ids["named"]] is False

    db_session.expire_all()
    assert db_session.get(VehicleVariant, dup.id).energy_type == "HEV", "撞键行不得被写库"
    assert db_session.get(VehicleVariant, good.id).energy_type == "PHEV", "正常行不受影响"
    assert db_session.get(VehicleVariant, ids["evidence"]).energy_type == "PHEV", "其余修复照常生效"

    # 复跑：其余为 0，冲突行仍被报告（等待人工去重）
    again = repair_energy_labels(db_session, apply=False)
    assert [c for c in again["changes"] if not c["conflict"]] == []
    assert [c["variant_id"] for c in again["skipped_conflicts"]] == [dup.id]
