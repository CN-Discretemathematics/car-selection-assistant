"""汽车之家 SKU（索引/参数配置）解析与导入测试。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import SpecFact, VehicleModelYear, VehicleSeries, VehicleVariant
from app.sources.autohome_sku import (
    build_sku_payload,
    classify_variant_energy,
    ev_evidence,
    has_engine_evidence,
    normalize_displacement,
    normalize_price_note,
    parse_series_index,
    parse_sku_config,
)
from app.sources.importer import import_catalog


def _index_fixture_html() -> str:
    return """
<html><body>
<dl id="33" olr="6">
  <dt><a href="//car.autohome.com.cn/price/brand-33.html#p"><img width="50" height="50"
     src="//car3.autoimg.cn/cardfs/brand/audi.png"></a>
     <div><a href="//car.autohome.com.cn/price/brand-33.html#p">奥迪</a></div></dt>
  <dd>
    <div class="h3-tit"><a href="//car.autohome.com.cn/price/brand-33-9.html">一汽奥迪</a></div>
    <ul class="rank-list-ul">
      <li id="s692">
        <h4><a href='//www.autohome.com.cn/692/#levelsource=0'>奥迪A4L</a></h4>
        <div>指导价：<a class='red' href='//www.autohome.com.cn/692/price.html'>28.98-36.28万</a></div>
        <div><a href='//car.autohome.com.cn/price/series-692.html'>参数</a></div>
      </li>
      <li id="s18">
        <h4><a href='//www.autohome.com.cn/18/'>奥迪A6L</a></h4>
        <div>指导价：<a class='red' href='//www.autohome.com.cn/18/price.html'>停售</a></div>
      </li>
    </ul>
  </dd>
</dl>
</body></html>
"""


def _sku_result_fixture() -> dict:
    return {
        "conditionlist": [
            {"typevalue": "year", "name": "年款", "list": [{"name": "2026款", "id": "2026"}]},
            {"typevalue": "displacement", "name": "排量", "list": [{"name": "新能源", "id": "新能源"}]},
            {"typevalue": "gearbox", "name": "变速箱", "list": [{"name": "自动", "id": "自动"}]},
            {"typevalue": "standards", "name": "环保标准", "list": [{"name": "国标", "id": "国标"}]},
            {"typevalue": "cartype", "name": "车体结构", "list": [{"name": "两厢车", "id": "两厢车"}]},
            {"typevalue": "drivemode", "name": "驱动方式", "list": [{"name": "前置前驱", "id": "前置前驱"}]},
            {"typevalue": "seatcount", "name": "座位数", "list": [{"name": "5座", "id": "5"}]},
        ],
        "paramitems": [
            {
                "itemtype": "基本信息",
                "groupname": "参数信息",
                "items": [
                    {
                        "name": "厂商指导价(元)",
                        "paramitemid": 113,
                        "modelexcessids": [
                            {"id": 77986, "value": "6.48万", "priceinfo": "-1"},
                            {"id": 77987, "value": "7.18万", "priceinfo": "-1"},
                        ],
                    },
                    {
                        "name": "长*宽*高(mm)",
                        "paramitemid": 120,
                        "modelexcessids": [
                            {"id": 77986, "value": "3381*1685*1721"},
                            {"id": 77987, "value": "3381*1685*1721"},
                        ],
                    },
                ],
            },
            {
                "itemtype": "电动机",
                "groupname": "参数信息",
                "items": [
                    {
                        "name": "电动机总功率(kW)",
                        "paramitemid": 200,
                        "modelexcessids": [
                            {"id": 77986, "value": "50"},
                            {"id": 77987, "value": "50"},
                        ],
                    },
                ],
            },
        ],
        "configitems": [
            {
                "itemtype": "安全配置",
                "groupname": "安全配置",
                "items": [
                    {
                        "name": "主/副驾驶座安全气囊",
                        "paramitemid": 1,
                        "modelexcessids": [
                            {"id": 77986, "value": "主驾 / 副驾"},
                            {"id": 77987, "value": "主驾 / 副驾"},
                        ],
                    },
                ],
            }
        ],
        "specinfo": {
            "specitems": [
                {
                    "specid": 77986, "pricetitle": "指导价：", "year": 2026,
                    "noshowprice": 64800, "minprice": "6.48万", "downprice": "",
                    "canaskprice": 0, "specname": "2026款 310km 向往版",
                    "condition": ["2026", "新能源", "自动", "国标", "两厢车", "前置前驱", "5"],
                    "specstatus": 20,
                },
                {
                    "specid": 77987, "pricetitle": "指导价：", "year": 2026,
                    "noshowprice": 71800, "minprice": "7.18万", "downprice": "",
                    "canaskprice": 0, "specname": "2026款 310km 乘风版",
                    "condition": ["2026", "新能源", "自动", "国标", "两厢车", "前置前驱", "5"],
                    "specstatus": 20,
                },
            ]
        },
    }


def test_parse_series_index():
    parsed = parse_series_index(_index_fixture_html())
    assert len(parsed["brands"]) == 1
    brand = parsed["brands"][0]
    assert brand["brand_id"] == "33"
    assert brand["name"] == "奥迪"
    assert brand["logo"] == "https://car3.autoimg.cn/cardfs/brand/audi.png"
    assert [s["external_id"] for s in brand["series"]] == ["692", "18"]
    assert brand["series"][0]["name"] == "奥迪A4L"
    assert brand["series"][0]["price_note"] == "28.98-36.28万元"
    assert brand["series"][1]["price_note"] is None  # 停售 → None


def test_normalize_price_note():
    assert normalize_price_note("16.59-20.99万") == "16.59-20.99万元"
    assert normalize_price_note("9.98万元") == "9.98万元"
    assert normalize_price_note("暂无") is None
    assert normalize_price_note("停售") is None
    assert normalize_price_note("") is None


def test_parse_sku_config():
    parsed = parse_sku_config(_sku_result_fixture())
    assert len(parsed["variants"]) == 2
    v = parsed["variants"][0]
    assert v["specid"] == "77986"
    assert v["year"] == 2026
    assert v["price_cny"] == 64800
    assert v["status"] == "on_sale"
    assert v["condition"][1] == "新能源"
    categories = {g["category"] for g in parsed["groups"]}
    assert categories == {"参数信息", "安全配置"}
    info_keys = set()
    for g in parsed["groups"]:
        if g["category"] == "参数信息":
            info_keys |= {i["key"] for i in g["items"]}
    assert info_keys == {"厂商指导价(元)", "长*宽*高(mm)", "电动机总功率(kW)"}
    dim = next(i for g in parsed["groups"] if g["category"] == "参数信息"
               for i in g["items"] if i["key"] == "长*宽*高(mm)")
    assert dim["values"]["77986"] == "3381*1685*1721"


def test_classify_variant_energy():
    assert classify_variant_energy("2026款 310km 向往版", "新能源", False, ["BEV"]) == "BEV"
    assert classify_variant_energy("2025款 DM-i 110km", "新能源", True, ["BEV", "PHEV", "EREV"]) == "PHEV"
    assert classify_variant_energy("2026款 增程 Max", "新能源", True, ["BEV", "EREV"]) == "EREV"
    assert classify_variant_energy("2025款 双擎 2.0L", "2.0L", True, ["ICE", "HEV"]) == "HEV"
    assert classify_variant_energy("2025款 40 TFSI", "2.0T", True, ["ICE"]) == "ICE"
    assert classify_variant_energy("理想L6 Pro", "新能源", True, ["BEV", "PHEV", "EREV"]) == "EREV"
    assert classify_variant_energy("问界M7 智驾版", "新能源", True, ["EREV"]) == "EREV"
    # 「插电/插电式混动」必须判为 PHEV（不得落入「混动」→ HEV）
    assert classify_variant_energy("2025款 1.5T 插电混动 豪华版", "1.5T", True, ["ICE", "PHEV"]) == "PHEV"
    assert classify_variant_energy("2025款 插电式混动 尊贵版", "新能源", True, ["PHEV"]) == "PHEV"
    assert classify_variant_energy("2025款 e:PHEV 智享版", "新能源", True, ["PHEV"]) == "PHEV"


def test_classify_variant_energy_vendor_naming():
    """评审 P1（用户实测）：厂商自有命名不得落到「有排量 → ICE」。

    线上事故：银河星耀8「225km EM-i」、星耀7「220km 四驱远航版」被标成 ICE，
    用户说「想买15万的燃油车」时推荐里出现插混车。
    """
    # 吉利雷神 EM-i / 领克 EM-P / 丰田双擎E+ → 插混；日产 e-POWER / 雷神 Hi·F → 油混
    assert classify_variant_energy("2026款 225km EM-i 远航家航海版", "1.5T", True, ["PHEV"]) == "PHEV"
    assert classify_variant_energy("2026款 EM-P Halo", "新能源", True, ["PHEV"]) == "PHEV"
    assert classify_variant_energy("2025款 双擎E+ 豪华版", "1.8L", True, ["HEV", "PHEV"]) == "PHEV"
    assert classify_variant_energy("2025款 e-POWER 超智联", "1.2L", True, ["HEV"]) == "HEV"
    assert classify_variant_energy("2025款 雷神Hi·F 智擎版", "1.5L", True, ["HEV"]) == "HEV"


def test_classify_variant_energy_ev_evidence():
    """款型名无任何标识词时，用「纯电续航 / 电池能量」证据细分（不得一律按燃油）。"""
    # 有发动机 + 纯电续航 220km → 插混（星耀7 实测场景）
    assert classify_variant_energy(
        "2026款 220km 四驱远航版", "1.5T", True, ["ICE"],
        ev_range_km=220.0, battery_kwh=18.4,
    ) == "PHEV"
    # 车系并集只标增程时，长纯电续航按增程理解
    assert classify_variant_energy(
        "2026款 四驱远航版", "1.5T", True, ["EREV"], ev_range_km=210.0,
    ) == "EREV"
    # 有发动机 + 小电池（无纯电续航）→ 油混
    assert classify_variant_energy(
        "2026款 智享版", "2.0L", True, ["ICE", "HEV"], battery_kwh=1.6,
    ) == "HEV"
    # 反向保护①：纯电款型不得因为有续航数字被判成插混
    assert classify_variant_energy(
        "2026款 310km 向往版", "新能源", False, ["BEV"],
        ev_range_km=310.0, battery_kwh=30.12,
    ) == "BEV"
    # 反向保护②：真燃油（启动电池按 Ah 标注、无纯电续航）仍判 ICE
    assert classify_variant_energy(
        "2025款 40 TFSI 豪华版", "2.0T", True, ["ICE"], battery_kwh=0.7,
    ) == "ICE"


def test_ev_evidence_from_facts():
    """电驱证据提取：纯电续航取最大值，Ah 标注的启动电池不计入 kWh。"""
    facts = [
        {"fact_key": "CLTC纯电续航里程(km)", "value": "220"},
        {"fact_key": "NEDC纯电续航里程(km)", "value": "180"},
        {"fact_key": "电池能量(kWh)", "value": "18.4"},
        {"fact_key": "电池容量(Ah)", "value": "60"},
        {"fact_key": "油箱容积(L)", "value": "45"},
        {"fact_key": "CLTC纯电续航里程(km)", "value": "-"},
    ]
    assert ev_evidence(facts) == (220.0, 18.4)
    assert ev_evidence([]) == (0.0, 0.0)
    # 评审 m4：Ah 写在**取值**里（键名不带单位）同样不算动力电池
    assert ev_evidence([{"fact_key": "电池容量", "value": "60Ah"}]) == (0.0, 0.0)
    assert ev_evidence([{"fact_key": "蓄电池容量", "value": "1.2kWh"}]) == (0.0, 1.2)


def test_normalize_displacement_and_engine_evidence():
    """排量占位值「0 / 0.0」等价于无发动机；Ah 启动电池不算发动机证据。"""
    assert normalize_displacement("0") == ""
    assert normalize_displacement("0.0") == ""
    assert normalize_displacement("0L") == ""
    assert normalize_displacement("新能源") == "新能源"
    assert normalize_displacement("1.5T") == "1.5T"
    assert normalize_displacement("1498") == "1498"  # mL 写法同样是真排量
    assert normalize_displacement(None) == ""

    assert has_engine_evidence([], "2.0T") is True
    assert has_engine_evidence([], "0") is False, "纯电占位排量不是发动机证据"
    assert has_engine_evidence([], "新能源") is False
    assert has_engine_evidence([{"fact_key": "发动机最大功率(kW)", "value": "120"}]) is True
    assert has_engine_evidence([{"fact_key": "排量(L)", "value": "0"}]) is False
    assert has_engine_evidence([{"fact_key": "电池容量(Ah)", "value": "60"}]) is False
    assert has_engine_evidence([{"fact_key": "发动机最大功率(kW)", "value": "-"}]) is False


def test_classify_variant_energy_no_false_positive():
    """真实数据回归：不得为了修插混而误伤纯电与燃油（生产快照抽样）。"""
    # ①「Premium」里的 EMI 子串：丰田bZ3 纯电 / 库斯途燃油都不得判成插混
    assert classify_variant_energy(
        "2024款 616km 长续航 Premium BEV", "新能源", False, ["BEV"],
    ) == "BEV"
    assert classify_variant_energy(
        "2024款 380TGDi LUX Premium 智爱尊贵版", "2.0T", True, ["ICE"],
    ) == "ICE"
    # ② 排量为「0」的纯电（大家7 527km / 小鹏G6 625 Max）：不得因「排量非新能源」判成带发动机
    assert classify_variant_energy(
        "2025款 改款 527km 草原长续航版", "0", False, ["BEV", "ICE"],
        ev_range_km=527.0, battery_kwh=195.0,
    ) == "BEV"
    assert classify_variant_energy(
        "2026款 625 Max 科技版", "0", False, ["BEV", "EREV"],
        ev_range_km=625.0, battery_kwh=68.5,
    ) == "BEV"
    # ③ 排量未知但确有发动机事实、且无任何电驱证据（房车/商用车）：判燃油而非插混
    assert classify_variant_energy("2022款 2.4T乐享版", "", True, ["BEV"]) == "ICE"
    # ④ 48V 轻混（奥迪智混，1.7kWh、无纯电续航）：判油混，不得判成插混
    assert classify_variant_energy(
        "2026款 2.0T 全域智混版 quattro", "2.0T", True, ["ICE", "HEV"],
        battery_kwh=1.7,
    ) == "HEV"


def test_condition_index_reorder_mapping():
    """conditionlist 顺序变化时按 typevalue 反查，业务键字段不错配。"""
    result = {
        "conditionlist": [
            {"typevalue": "drivemode", "name": "驱动方式", "list": [{"name": "前置前驱", "id": "前置前驱"}]},
            {"typevalue": "cartype", "name": "车体结构", "list": [{"name": "SUV", "id": "SUV"}]},
            {"typevalue": "displacement", "name": "排量", "list": [{"name": "新能源", "id": "新能源"}]},
        ],
        "paramitems": [],
        "configitems": [],
        "specinfo": {
            "specitems": [
                {
                    "specid": 1, "year": 2026, "noshowprice": 100000,
                    "specname": "2026款 增程 四驱版",
                    "condition": ["前置前驱", "SUV", "新能源"], "specstatus": 20,
                }
            ]
        },
    }
    parsed = parse_sku_config(result)
    assert parsed["condition_index"] == {"drivemode": 0, "cartype": 1, "displacement": 2}
    payload = build_sku_payload(
        {"name": "测试品牌", "brand_type": "domestic_nev", "inclusion_reason": "测试"},
        {"external_id": "9999", "name": "测试车系", "energy_types": ["EREV"]},
        parsed,
        "https://car.m.autohome.com.cn/config/series/9999.html",
    )
    variant = payload["series"][0]["model_years"][0]["variants"][0]
    assert variant["drivetrain"] == "前置前驱"
    assert variant["powertrain"] == "增程式"
    assert variant["energy_type"] == "EREV"
    assert variant["body_type"] == "suv"
    assert variant["price_cny"] == 100000


def test_parse_fact_unit_cycle():
    """从配置项键名解析单位与工况（评审 M5）。"""
    from app.sources.autohome_sku import parse_fact_unit_cycle

    assert parse_fact_unit_cycle("CLTC纯电续航里程(km)") == ("km", "CLTC")
    assert parse_fact_unit_cycle("长*宽*高(mm)") == ("mm", None)
    assert parse_fact_unit_cycle("电动机总功率(kW)") == ("kW", None)
    assert parse_fact_unit_cycle("WLTC综合油耗(L/100km)") == ("L/100km", "WLTC")
    assert parse_fact_unit_cycle("座位数(个)") == ("个", None)
    assert parse_fact_unit_cycle("驱动方式") == (None, None)


def test_sku_import_roundtrip(db_session: Session):
    parsed = parse_sku_config(_sku_result_fixture())
    payload = build_sku_payload(
        {"name": "宝骏", "brand_type": "domestic_nev", "inclusion_reason": "测试"},
        {"external_id": "7806", "name": "宝骏悦也", "energy_types": ["BEV"],
         "price_note": "6.48-9.48万元"},
        parsed,
        "https://car.m.autohome.com.cn/config/series/7806.html",
    )
    assert payload["source"]["source_type"] == "industry_data"
    series_cfg = payload["series"][0]
    assert series_cfg["name"] == "宝骏悦也"
    assert series_cfg["body_type"] == "sedan"  # 两厢车 → sedan
    assert series_cfg["energy_types"] == ["BEV"]
    years = {y["year_name"]: y for y in payload["series"][0]["model_years"]}
    assert set(years) == {"2026款"}
    variants = years["2026款"]["variants"]
    assert len(variants) == 2
    v = variants[0]
    assert v["config_version"] == "310km 向往版"
    assert v["powertrain"] == "纯电动"
    assert v["drivetrain"] == "前置前驱"
    assert v["energy_type"] == "BEV"
    assert v["price_cny"] == 64800
    assert v["status"] == "on_sale"
    keys = {f["category"] + "/" + f["fact_key"] for f in v["facts"]}
    assert "参数信息/长*宽*高(mm)" in keys
    assert "安全配置/主/副驾驶座安全气囊" in keys
    # 评审 M6：座位数写入结构化 fact（Agent 乘客硬约束依赖）
    seat = next(f for f in v["facts"] if f["fact_key"] == "座位数(个)")
    assert seat["value"] == "5" and seat["unit"] == "个"

    report = import_catalog(db_session, payload)
    assert report.ok, report.errors
    series = db_session.scalars(select(VehicleSeries).where(VehicleSeries.name == "宝骏悦也")).first()
    assert series is not None
    assert series.body_type == "sedan"
    assert series.price_range_note == "6.48-9.48万元"
    years_db = db_session.scalars(
        select(VehicleModelYear).where(VehicleModelYear.series_id == series.id)
    ).all()
    assert len(years_db) == 1
    variants_db = db_session.scalars(
        select(VehicleVariant).where(VehicleVariant.series_id == series.id)
    ).all()
    assert len(variants_db) == 2
    facts = db_session.scalars(
        select(SpecFact).where(SpecFact.variant_id == variants_db[0].id)
    ).all()
    assert len(facts) == 5  # 原 4 条 + 座位数
    assert report.created.get("fact", 0) == 10

    # 重复导入：不产生重复 SKU/事实
    report2 = import_catalog(db_session, payload)
    assert report2.ok
    assert len(db_session.scalars(select(VehicleVariant)).all()) == 2
    assert len(db_session.scalars(select(SpecFact)).all()) == 10


def test_sku_reimport_updates_status(db_session: Session):
    """同源重复导入：款型状态与年款上市状态随最新数据更新。"""
    parsed = parse_sku_config(_sku_result_fixture())
    base = {
        "brand": {"name": "宝骏", "brand_type": "domestic_nev", "inclusion_reason": "测试"},
        "series": {"external_id": "7806", "name": "宝骏悦也", "energy_types": ["BEV"],
                   "price_note": "6.48-9.48万元"},
        "parsed": parsed,
        "url": "https://car.m.autohome.com.cn/config/series/7806.html",
    }
    assert import_catalog(db_session, build_sku_payload(base["brand"], base["series"], base["parsed"], base["url"])).ok

    # 第二次导入：全部停售
    parsed2 = parse_sku_config(_sku_result_fixture())
    for v in parsed2["variants"]:
        v["status"] = "off_sale"
    report = import_catalog(db_session, build_sku_payload(base["brand"], base["series"], parsed2, base["url"]))
    assert report.ok, report.errors
    series = db_session.scalars(select(VehicleSeries).where(VehicleSeries.name == "宝骏悦也")).first()
    year = db_session.scalars(
        select(VehicleModelYear).where(VehicleModelYear.series_id == series.id)
    ).first()
    assert year.launch_status == "discontinued"
    variants = db_session.scalars(
        select(VehicleVariant).where(VehicleVariant.series_id == series.id)
    ).all()
    assert all(v.status == "off_sale" for v in variants)
