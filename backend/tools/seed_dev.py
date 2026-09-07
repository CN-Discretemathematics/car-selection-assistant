"""开发环境样例数据（仅限本地 dev.db，禁止用于生产或测试断言正式数据）。

纪律：不得用模拟数据冒充正式数据来源。
本脚本创建的数据来源明确标记为「本地开发样例（非正式数据源）」，只用于
本地联调 UI（首页卡片 / 详情 / 对比）。生产数据一律由数据管线 + 真实来源生成。
"""
from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.catalog.services import latest_full_month  # noqa: E402
from app.common.database import create_all, get_session_factory  # noqa: E402
from app.common.models import (  # noqa: E402
    Brand,
    MonthlySales,
    OfficialPrice,
    Source,
    SpecFact,
    VehicleModelYear,
    VehicleSeries,
    VehicleVariant,
)

DEV_SOURCE_NAME = "本地开发样例（非正式数据源）"

SEED = {
    "示例新能源": {
        "brand_type": "domestic_nev",
        "series": [
            {
                "name": "星云SUV",
                "body_type": "suv",
                "energy_types": ["BEV", "PHEV"],
                "positioning": "中型家用SUV",
                "sales": [("2025-01", 18600), ("2026-06", 22100), ("2026-07", 24350)],
                "years": {
                    "2025款": [
                        {
                            "config_version": "标准版",
                            "powertrain": "纯电",
                            "drivetrain": "后驱",
                            "energy_type": "BEV",
                            "price": "219800",
                            "facts": [
                                ("动力", "power_kw", "220", "kW", None),
                                ("电池和续航", "range_km", "605", "km", "CLTC"),
                                ("座位数", "seats", "5", "座", None),
                                ("尺寸", "length_mm", "4820", "mm", None),
                            ],
                        },
                        {
                            "config_version": "长续航版",
                            "powertrain": "纯电",
                            "drivetrain": "后驱",
                            "energy_type": "BEV",
                            "price": "249800",
                            "facts": [
                                ("动力", "power_kw", "220", "kW", None),
                                ("电池和续航", "range_km", "755", "km", "CLTC"),
                                ("座位数", "seats", "5", "座", None),
                                ("尺寸", "length_mm", "4820", "mm", None),
                            ],
                        },
                        {
                            "config_version": "插混四驱版",
                            "powertrain": "插电混动",
                            "drivetrain": "四驱",
                            "energy_type": "PHEV",
                            "price": "259800",
                            "facts": [
                                ("动力", "power_kw", "310", "kW", None),
                                ("电池和续航", "range_km", "180", "km", "CLTC"),
                                ("油耗或能耗", "fuel_consumption", "5.8", "L/100km", "WLTC"),
                                ("座位数", "seats", "5", "座", None),
                            ],
                        },
                    ],
                    "2026款": [
                        {
                            "config_version": "智驾版",
                            "powertrain": "纯电",
                            "drivetrain": "后驱",
                            "energy_type": "BEV",
                            "price": "239800",
                            "facts": [
                                ("动力", "power_kw", "230", "kW", None),
                                ("电池和续航", "range_km", "680", "km", "CLTC"),
                                ("智能驾驶", "city_noa", "支持", None, None),
                                ("座位数", "seats", "5", "座", None),
                            ],
                        },
                    ],
                },
            },
            {
                "name": "云雀家轿",
                "body_type": "sedan",
                "energy_types": ["BEV"],
                "positioning": "紧凑型家轿",
                "sales": [("2026-06", 15500), ("2026-07", 16800)],
                "years": {
                    "2025款": [
                        {
                            "config_version": "标准版",
                            "powertrain": "纯电",
                            "drivetrain": "前驱",
                            "energy_type": "BEV",
                            "price": "129800",
                            "facts": [
                                ("动力", "power_kw", "150", "kW", None),
                                ("电池和续航", "range_km", "520", "km", "CLTC"),
                                ("尺寸", "length_mm", "4680", "mm", None),
                            ],
                        },
                        {
                            "config_version": "旗舰版",
                            "powertrain": "纯电",
                            "drivetrain": "前驱",
                            "energy_type": "BEV",
                            "price": "159800",
                            "facts": [
                                ("动力", "power_kw", "150", "kW", None),
                                ("电池和续航", "range_km", "610", "km", "CLTC"),
                                ("智能座舱", "hud", "支持", None, None),
                                ("尺寸", "length_mm", "4680", "mm", None),
                            ],
                        },
                    ],
                },
            },
        ],
    },
    "示例合资": {
        "brand_type": "other_fuel",
        "series": [
            {
                "name": "骏驰B级车",
                "body_type": "sedan",
                "energy_types": ["ICE", "HEV"],
                "positioning": "中型轿车",
                "sales": [("2026-06", 9800), ("2026-07", 10500)],
                "years": {
                    "2025款": [
                        {
                            "config_version": "2.0L 豪华版",
                            "powertrain": "2.0L 汽油",
                            "drivetrain": "前驱",
                            "energy_type": "ICE",
                            "price": "179800",
                            "facts": [
                                ("动力", "power_kw", "137", "kW", None),
                                ("油耗或能耗", "fuel_consumption", "6.2", "L/100km", "WLTC"),
                                ("座位数", "seats", "5", "座", None),
                            ],
                        },
                        {
                            "config_version": "2.5L 双擎版",
                            "powertrain": "2.5L 油电混动",
                            "drivetrain": "前驱",
                            "energy_type": "HEV",
                            "price": "219800",
                            "facts": [
                                ("动力", "power_kw", "160", "kW", None),
                                ("油耗或能耗", "fuel_consumption", "4.4", "L/100km", "WLTC"),
                                ("座位数", "seats", "5", "座", None),
                            ],
                        },
                    ],
                },
            },
        ],
    },
}


def _seed(db: Session) -> None:
    existing = db.scalars(select(Source).where(Source.name == DEV_SOURCE_NAME)).first()
    if existing is not None:
        print("样例数据已存在，跳过（如需重建请先删除 dev.db 后重新 alembic upgrade）。")
        return

    source = Source(
        name=DEV_SOURCE_NAME,
        source_type="other",
        url=None,
        verified_status="unverified",
        credibility="low",
    )
    db.add(source)
    db.flush()

    for brand_name, brand_cfg in SEED.items():
        brand = Brand(
            name=brand_name,
            brand_type=brand_cfg["brand_type"],
            official_site=None,
            inclusion_reason="本地开发样例",
            source_id=source.id,
        )
        db.add(brand)
        db.flush()

        for series_cfg in brand_cfg["series"]:
            series = VehicleSeries(
                brand_id=brand.id,
                name=series_cfg["name"],
                body_type=series_cfg["body_type"],
                energy_types=series_cfg["energy_types"],
                positioning=series_cfg["positioning"],
                official_page_url="https://example.com/vehicles/demo",  # 本地演示用示例链接（非真实官网）
                active_status="active",
                source_id=source.id,
            )
            db.add(series)
            db.flush()

            for year_name, variants in series_cfg["years"].items():
                year = VehicleModelYear(
                    series_id=series.id,
                    year_name=year_name,
                    launch_status="on_sale",
                    source_id=source.id,
                )
                db.add(year)
                db.flush()
                for v in variants:
                    variant = VehicleVariant(
                        series_id=series.id,
                        model_year_id=year.id,
                        display_name=f"{brand_name} {series_cfg['name']} {year_name} {v['config_version']}",
                        config_version=v["config_version"],
                        powertrain=v["powertrain"],
                        drivetrain=v["drivetrain"],
                        energy_type=v["energy_type"],
                        body_type=series_cfg["body_type"],
                        status="on_sale",
                        effective_from=date(2025, 1, 1),
                        source_id=source.id,
                    )
                    db.add(variant)
                    db.flush()
                    db.add(
                        OfficialPrice(
                            variant_id=variant.id,
                            price_cny=Decimal(v["price"]),
                            price_type="official_msrp",
                            effective_from=date(2025, 1, 1),
                            source_id=source.id,
                        )
                    )
                    for category, key, value, unit, cycle in v["facts"]:
                        db.add(
                            SpecFact(
                                variant_id=variant.id,
                                category=category,
                                fact_key=key,
                                fact_value=value,
                                unit=unit,
                                cycle=cycle,
                                source_id=source.id,
                            )
                        )

            for month, count in series_cfg["sales"]:
                db.add(
                    MonthlySales(
                        series_id=series.id,
                        month=month,
                        sales_type="retail",
                        sales_count=count,
                        source_id=source.id,
                    )
                )

    db.commit()
    print(f"样例数据已写入 dev.db（来源：{DEV_SOURCE_NAME}）")
    print(f"最近完整自然月：{latest_full_month()}；启动后端后可访问 http://127.0.0.1:8000/api/v1/home 查看。")


def main() -> None:
    create_all()
    factory = get_session_factory()
    with factory() as db:
        _seed(db)


if __name__ == "__main__":
    main()
