"""具体车系问答测试（app/agent/series_qa.py 解析/判定/回答 + 引擎接入）。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.series_qa import (
    asks_variant_diff,
    build_series_qa_answer,
    build_variant_diff_answer,
    negates_series,
    should_answer,
)
from app.catalog.series_index import normalize_name, resolve_series, series_headline
from app.catalog.services import latest_full_month
from app.common.enums import MISSING_VALUE_LABEL
from app.common.models import Brand, VehicleSeries
from tests.seed import make_brand, make_sales, make_series, make_source, make_variant, make_year


def _seed_two_series(db_session: Session) -> dict[str, int]:
    source = make_source(db_session, name="汽车之家")
    brand_z = make_brand(db_session, name="腾势", source=source)
    brand_t = make_brand(db_session, name="丰田", source=source)
    z9 = make_series(db_session, brand_z, name="腾势Z9GT", body_type="sedan",
                     energy_types=("BEV", "ICE"), source=source)
    z9.positioning = "中大型车"
    raf = make_series(db_session, brand_t, name="卡罗拉锐放", body_type="suv",
                      energy_types=("HEV", "ICE"), source=source)
    raf.positioning = "紧凑型SUV"

    year_z = make_year(db_session, z9)
    year_r = make_year(db_session, raf)
    make_variant(
        db_session, z9, year_z, config_version="四驱版", energy_type="BEV", price_cny="339800",
        facts=[
            ("参数信息", "电动机总功率(kW)", "710", "kW", None),
            ("参数信息", "CLTC综合续航(km)", "710", "km", "CLTC"),
            ("参数信息", "官方0-100km/h加速(s)", "3.5", "s", None),
            ("参数信息", "长*宽*高(mm)", "5180*1990*1500", None, None),
            ("操控配置", "空气悬架", "有", None, None),
        ],
        source=source,
    )
    make_variant(
        db_session, raf, year_r, config_version="双擎版", energy_type="HEV", price_cny="129800",
        facts=[
            ("参数信息", "系统综合功率(kW)", "144", "kW", None),
            ("参数信息", "WLTC综合油耗(L/100km)", "4.56", "L/100km", "WLTC"),
            ("参数信息", "长*宽*高(mm)", "4460*1825*1620", None, None),
            ("安全配置", "主动刹车/主动安全系统", "有", None, None),
        ],
        source=source,
    )
    make_sales(db_session, z9, latest_full_month(), 2000, source=source)
    make_sales(db_session, raf, latest_full_month(), 3000, source=source)
    db_session.commit()
    return {"z9": z9.id, "raf": raf.id}


def test_normalize_name():
    assert normalize_name("腾势Z9 GT") == "腾势z9gt"
    assert normalize_name("丰田-卡罗拉锐放") == "丰田卡罗拉锐放"
    assert normalize_name("腾势·Z9GT") == "腾势z9gt"


def test_resolve_and_dedupe_longest_wins(db_session: Session):
    ids = _seed_two_series(db_session)
    resolved = resolve_series(db_session, "腾势z9GT和丰田卡罗拉锐放相比有什么优点？")
    assert [s.id for s, _ in resolved] == [ids["z9"], ids["raf"]]
    # 品牌+车系名与去品牌前缀均能命中
    assert [s.id for s, _ in resolve_series(db_session, "Z9GT怎么样")] == [ids["z9"]]
    assert [s.id for s, _ in resolve_series(db_session, "丰田卡罗拉锐放怎么样")] == [ids["raf"]]
    assert resolve_series(db_session, "帮我推荐20万以内的SUV") == []


def test_should_answer_rules(db_session: Session):
    ids = _seed_two_series(db_session)
    two = resolve_series(db_session, "腾势z9GT和卡罗拉锐放相比")
    assert len(two) == 2 and should_answer(two, "腾势z9GT和卡罗拉锐放相比")
    one = resolve_series(db_session, "卡罗拉锐放有什么优点")
    assert should_answer(one, "卡罗拉锐放有什么优点")
    one_plain = resolve_series(db_session, "卡罗拉锐放推荐吗")
    assert should_answer(one_plain, "卡罗拉锐放推荐吗")
    # 单车系但无提问触发词（明确走推荐链路）
    no_trig = resolve_series(db_session, "帮我推荐卡罗拉锐放")
    assert not should_answer(no_trig, "帮我推荐卡罗拉锐放")


def test_should_answer_parameter_questions(db_session: Session):
    """评审 M-R10：参数提问句式（续航多少/有没有X/油耗/轴距）路由到车系问答。"""
    _seed_two_series(db_session)
    for msg in ("腾势Z9GT续航多少", "腾势Z9GT有没有空气悬架", "卡罗拉锐放油耗是多少", "腾势Z9GT轴距多长"):
        resolved = resolve_series(db_session, msg)
        assert resolved, msg
        assert should_answer(resolved, msg), msg
    # 否定语境仍然不做问答
    neg = resolve_series(db_session, "我不买腾势Z9GT，油耗太高了")
    assert neg and not should_answer(neg, "我不买腾势Z9GT，油耗太高了")


def test_probe_answers_parameter_from_db(db_session: Session):
    """评审 M-R10：按需参数查找直接答出 DB 全量事实（绕过 RAG 索引采样上限）。"""
    _seed_two_series(db_session)
    resolved = resolve_series(db_session, "腾势Z9GT续航多少")
    answer = build_series_qa_answer(db_session, resolved, "腾势Z9GT续航多少")
    assert "你问到的相关参数" in answer
    assert "CLTC综合续航" in answer and "710" in answer

    resolved2 = resolve_series(db_session, "腾势Z9GT有没有空气悬架")
    answer2 = build_series_qa_answer(db_session, resolved2, "腾势Z9GT有没有空气悬架")
    assert "空气悬架" in answer2 and "空气悬架 = 有" in answer2

    # 双车系对比语境同样回答问到的参数
    resolved3 = resolve_series(db_session, "腾势Z9GT和卡罗拉锐放油耗对比")
    answer3 = build_series_qa_answer(db_session, resolved3, "腾势Z9GT和卡罗拉锐放油耗对比")
    assert "WLTC综合油耗" in answer3 and "4.56" in answer3

    # 泛化提问（无维度关键词）不追加参数段，回答保持原样结构
    resolved4 = resolve_series(db_session, "卡罗拉锐放有什么优点")
    answer4 = build_series_qa_answer(db_session, resolved4, "卡罗拉锐放有什么优点")
    assert "你问到的相关参数" not in answer4


def test_headline_uses_extrema_and_features(db_session: Session):
    ids = _seed_two_series(db_session)
    z9 = series_headline(db_session, next(s for s, _ in resolve_series(db_session, "腾势Z9GT")))
    assert z9["动力"] == "710 kW"
    assert z9["续航"] == "710 km（CLTC）"
    assert z9["加速"] == "3.5 s"
    raf = series_headline(db_session, next(s for s, _ in resolve_series(db_session, "卡罗拉锐放")))
    assert raf["油耗"] == "4.56 L/100km（WLTC）"


def test_engine_comparison_answer(client: TestClient, db_session: Session):
    _seed_two_series(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    resp = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "腾势z9GT和丰田卡罗拉锐放相比有什么优点？"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["need_clarification"] is False
    assert body["recommended_variants"] == []
    text = body["explanation"]
    assert "腾势Z9GT" in text and "卡罗拉锐放" in text
    assert "官方指导价" in text and "万元" in text
    assert "参数对比" in text and "动力" in text
    assert body["citations"], "应带来源引用"
    assert body["recommended_series_ids"]  # 命中车系对外暴露


def test_engine_single_series_advantage(client: TestClient, db_session: Session):
    _seed_two_series(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    body = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "卡罗拉锐放有什么优点？"},
    ).json()
    assert body["need_clarification"] is False
    assert "卡罗拉锐放" in body["explanation"]
    assert "核心参数" in body["explanation"]
    assert "4.56" in body["explanation"]


def _seed_star_wish_scene(db_session: Session) -> dict[str, int]:
    """星愿 vs 零跑A10 vs 干扰项（用于「指定车型锁定」场景）。"""
    source = make_source(db_session, name="汽车之家")
    brand_ge = make_brand(db_session, name="银河", source=source)
    brand_lp = make_brand(db_session, name="零跑", source=source)
    brand_dc = make_brand(db_session, name="其他", source=source)
    star = make_series(db_session, brand_ge, name="星愿", body_type="sedan", energy_types=("BEV",), source=source)
    star.positioning = "小型车"
    a10 = make_series(db_session, brand_lp, name="零跑A10", body_type="suv", energy_types=("BEV",), source=source)
    a10.positioning = "小型SUV"
    decoy = make_series(db_session, brand_dc, name="干扰SUV", body_type="suv", energy_types=("ICE",), source=source)
    decoy.positioning = "紧凑型SUV"
    for series, price in ((star, "74000"), (a10, "89000"), (decoy, "95000")):
        year = make_year(db_session, series)
        make_variant(
            db_session, series, year,
            facts=[("参数信息", "座位数(个)", "5", "个", None), ("参数信息", "长*宽*高(mm)", "4500*1800*1600", None, None)],
            price_cny=price, source=source,
        )
    db_session.commit()
    return {"star": star.id, "a10": a10.id, "decoy": decoy.id}


def test_series_lock_until_unlock(client: TestClient, db_session: Session):
    """指定车型后只推荐该系列；「看看其他的」解锁（评审：用户指定车型时只关注指定车型）。"""
    ids = _seed_star_wish_scene(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]

    # 1) 无购车关键词也要能回答：「星愿和零跑A10选哪个」
    first = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "星愿和零跑A10选哪个"},
    ).json()
    assert first["need_clarification"] is False
    assert "星愿" in first["explanation"] and "零跑A10" in first["explanation"]

    # 2) 补齐画像 → 推荐只能从已点名的两个车系中出
    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "预算10万，自己用，1个人"},
    ).json()
    assert rec["recommended_variants"], "画像齐全应有推荐"
    assert {v["series_id"] for v in rec["recommended_variants"]} <= {ids["star"], ids["a10"]}, \
        "已指定车型时应只推荐该系列"
    assert all("数据源" not in (t or "") for v in rec["recommended_variants"] for t in v["tradeoffs"])

    # 3) 「看看其他的SUV」→ 解锁，干扰项进入候选
    broad = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "预算10万，看看其他的SUV"},
    ).json()
    series_ids = {v["series_id"] for v in broad["recommended_variants"]}
    assert ids["decoy"] in series_ids, "解锁后应回到广泛推荐（含干扰项）"


def test_unlock_not_triggered_by_another_word(client: TestClient, db_session: Session):
    """评审 P1：「另外…」是话语词不是「看其他车」，不得清空锁定。"""
    ids = _seed_star_wish_scene(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "星愿和零跑A10选哪个"},
    )
    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "另外预算10万，自己用，1个人"},
    ).json()
    recs = {v["series_id"] for v in rec["recommended_variants"]}
    assert recs and recs <= {ids["star"], ids["a10"]}, "「另外」不应解锁，仍只推荐点名车系"


def test_negation_no_qa_and_no_lock(client: TestClient, db_session: Session):
    """评审 P2：「我不买星愿」——不做该车问答、不锁定该车。"""
    ids = _seed_star_wish_scene(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "我不买星愿"},
    ).json()
    assert "关于" not in (out["explanation"] or ""), "否定语境不应触发车系问答"
    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "预算10万，自己用，1个人"},
    ).json()
    recs = {v["series_id"] for v in rec["recommended_variants"]}
    assert ids["decoy"] in recs, "星愿未被锁定，推荐应为广泛范围（含干扰项）"


def _seed_star_wish_versions(db_session: Session) -> dict:
    """星愿三个版本（310/410/480km）+ 一款燃油车（版本差异与锁定冲突场景）。"""
    source = make_source(db_session, name="汽车之家")
    brand_ge = make_brand(db_session, name="银河", source=source)
    brand_tc = make_brand(db_session, name="丰田", source=source)
    star = make_series(db_session, brand_ge, name="星愿", body_type="sedan",
                       energy_types=("BEV",), source=source)
    star.positioning = "小型车"
    corolla = make_series(db_session, brand_tc, name="卡罗拉", body_type="sedan",
                          energy_types=("ICE",), source=source)
    corolla.positioning = "紧凑型车"

    year_star = make_year(db_session, star)
    versions = (
        ("310km青春版", "73800", "310", "30.1", "70", "无"),
        ("410km精英版", "83800", "410", "40.2", "70", "无"),
        ("480km旗舰版", "95800", "480", "47.5", "100", "有"),
    )
    star_variant_ids: list[int] = []
    for config_version, price, range_km, battery, power, air_susp in versions:
        facts = [
            ("参数信息", "CLTC综合续航(km)", range_km, "km", "CLTC"),
            ("参数信息", "电池能量(kWh)", battery, "kWh", None),
            ("参数信息", "电动机总功率(kW)", power, "kW", None),
            ("参数信息", "长*宽*高(mm)", "4135*1805*1570", None, None),
            ("参数信息", "座位数(个)", "5", "个", None),
            ("操控配置", "空气悬架", air_susp, None, None),
        ]
        if config_version == "480km旗舰版":
            # 评审 M2：汽车之家对未配备的款型填「-」，导入层按占位值跳过，
            # 因此「顶配独有配置」在库里只有 1 行事实——这类差异必须被列出来
            facts.append(("选装配置", "车载冰箱", "有", None, None))
        variant = make_variant(
            db_session, star, year_star, config_version=config_version,
            energy_type="BEV", price_cny=price, source=source,
            facts=facts,
        )
        star_variant_ids.append(variant.id)

    year_c = make_year(db_session, corolla)
    corolla_variant = make_variant(
        db_session, corolla, year_c, config_version="1.5L精英版", powertrain="燃油",
        energy_type="ICE", price_cny="128800", source=source,
        facts=[
            ("参数信息", "座位数(个)", "5", "个", None),
            ("参数信息", "WLTC综合油耗(L/100km)", "5.6", "L/100km", "WLTC"),
        ],
    )
    db_session.commit()
    return {
        "star": star.id,
        "corolla": corolla.id,
        "star_variants": star_variant_ids,
        "corolla_variant": corolla_variant.id,
    }


def test_asks_variant_diff_trigger():
    """「版本/款型」+「区别/怎么选」才触发版本级问答，普通提问不误触发。"""
    for msg in (
        "星愿不同版本有什么区别",
        "星愿各版本差异大吗",
        "410km版本和310km版本哪个好",
        "这款车的不同款型怎么选",
        "星愿各版本续航差多少",
    ):
        assert asks_variant_diff(msg), msg
    for msg in (
        "星愿有什么优点",
        "帮我推荐15万以内的SUV",
        "星愿和零跑A10选哪个",
        "15万买哪台车好",
    ):
        assert not asks_variant_diff(msg), msg


def test_variant_diff_answer_lists_versions(client: TestClient, db_session: Session):
    """用户反馈 P1：「比较不同版本的差异」必须给出版本级差异，而不是「没有数据」。"""
    ids = _seed_star_wish_versions(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "星愿不同版本有什么区别"},
    ).json()
    text = out["explanation"] or ""
    assert out["need_clarification"] is False
    assert "没有数据" not in text and "无可靠数据" not in text
    assert "在售 3 个版本" in text
    assert "版本差异" in text
    # 三个版本的指导价 + 差异项取值（续航/电池/功率）都出现在回答里
    for token in ("7.38 万元", "8.38 万元", "9.58 万元", "310", "410", "480", "30.1", "47.5", "100"):
        assert token in text, token
    # 相同参数隐藏，但以「各版本一致」给出尺寸/座位，确认是同一车系的不同版本
    assert "各版本一致" in text and "4135*1805*1570" in text
    # 候选卡片＝三个版本（可直接加入对比）；非评分场景 score=0，前端不显示匹配分
    assert {v["variant_id"] for v in out["recommended_variants"]} == set(ids["star_variants"])
    assert all(v["score"] == 0 for v in out["recommended_variants"])
    assert out["filters"]["series_id"] == ids["star"]


def test_variant_diff_includes_partial_coverage_facts(db_session: Session):
    """评审 M2：只有部分款型收录到的事实（顶配独有配置）也必须算版本差异。

    真实口径：汽车之家对未配备款型填「-」，导入层跳过 → 库里只剩顶配那一行事实。
    旧实现只比较「已有该键的款型」，会把这类最常见的差异整片漏掉。
    """
    ids = _seed_star_wish_versions(db_session)
    series = db_session.get(VehicleSeries, ids["star"])
    brand = db_session.get(Brand, series.brand_id)
    text, _rows = build_variant_diff_answer(db_session, series, brand, "星愿各版本差在哪")

    line = next((ln for ln in text.splitlines() if "车载冰箱" in ln), "")
    assert line, f"顶配独有配置未出现在版本差异中：\n{text}"
    # 三个版本都要有取值：顶配「有」，两个低配如实标注未收录（不猜「无」）
    assert "有" in line
    assert line.count(MISSING_VALUE_LABEL) == 2
    # 页脚仍需声明「未收录 ≠ 没有该配置」
    assert "不代表没有该配置" in text


def test_variant_diff_answer_without_variant_data(db_session: Session):
    """库内无在售款型时如实说明并给官网入口，绝不编造版本差异。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="银河", source=source)
    series = make_series(db_session, brand, name="星愿", body_type="sedan",
                         energy_types=("BEV",), source=source)
    db_session.commit()
    text, rows = build_variant_diff_answer(db_session, series, brand, "星愿不同版本有什么区别")
    assert rows == []
    assert "暂未收录在售款型数据" in text
    assert "example.com/series" in text


def test_variant_diff_via_locked_series(client: TestClient, db_session: Session):
    """选中星愿后只说「不同版本的差异」（不带车名）也要能回答。"""
    ids = _seed_star_wish_versions(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "我在看银河星愿"},
    )
    out = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "帮我比较一下不同版本的差异"},
    ).json()
    text = out["explanation"] or ""
    assert "星愿" in text and "在售 3 个版本" in text
    assert {v["variant_id"] for v in out["recommended_variants"]} == set(ids["star_variants"])


def test_conflicting_need_releases_series_lock(client: TestClient, db_session: Session):
    """用户反馈 P1：锁定纯电星愿后改问「15万的燃油车」，不得仍把参数限定在星愿。"""
    ids = _seed_star_wish_versions(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    first = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "银河星愿怎么样"},
    ).json()
    assert first["recommended_series_ids"] == [ids["star"]], "首轮应命中星愿车系问答（并锁定）"

    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "我想买一台15万的燃油车，家用，平时2个人坐"},
    ).json()
    series_ids = {v["series_id"] for v in rec["recommended_variants"]}
    assert rec["recommended_variants"], "冲突解锁后应给出全市场候选，而不是空结果"
    assert ids["corolla"] in series_ids, "应能推荐到符合新需求的燃油车"
    assert ids["star"] not in series_ids, "纯电车系不应再进入候选"
    assert rec["filters"]["locked_series_ids"] == []
    assert any("已解除对" in r for r in rec["reasons"]), "口径变化必须对用户可见"


def test_lock_kept_when_new_need_still_fits(client: TestClient, db_session: Session):
    """反向保护：新需求与锁定车系不冲突时不得误解锁。"""
    ids = _seed_star_wish_versions(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "银河星愿怎么样"},
    )
    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "预算10万以内，纯电，通勤用，2个人"},
    ).json()
    assert rec["filters"]["locked_series_ids"] == [ids["star"]]
    assert {v["series_id"] for v in rec["recommended_variants"]} == {ids["star"]}


def test_unlock_note_survives_clarification_turn(client: TestClient, db_session: Session):
    """评审 M1：冲突约束在「被追问」的那一轮到达时，解锁与告知必须落在同一次回复。

    旧实现把探测放在追问分支之前：第一轮就已解锁并持久化，但回复只是追问，
    说明被丢弃，用户下一轮直接看到全市场推荐却不知道口径变了。
    现在探测后置到推荐分支之前，且判定基准是**累积画像**（不是本轮提示词），
    所以补齐信息的那一轮会解锁并明确告知。
    """
    ids = _seed_star_wish_versions(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "银河星愿怎么样"},
    )

    ask = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "我想买一台15万的燃油车"},  # 缺用途/人数 → 本轮追问
    ).json()
    assert ask["need_clarification"] is True, "缺项时应先追问，而不是硬推"
    assert not ask.get("recommended_variants"), "追问轮不应给推荐"

    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "家用，平时2个人坐"},  # 本轮无硬约束词，靠累积画像判定
    ).json()
    assert rec["filters"]["locked_series_ids"] == [], "补齐信息后应解除与画像冲突的锁定"
    assert any("已解除对" in r for r in rec["reasons"]), "口径变化必须与解锁同一次回复可见"
    series_ids = {v["series_id"] for v in rec["recommended_variants"]}
    assert ids["corolla"] in series_ids and ids["star"] not in series_ids


def test_negation_of_locked_series_unlocks(client: TestClient, db_session: Session):
    """评审 m2：「我不买星愿了，想要15万的燃油车」必须解锁，不得给空推荐。"""
    ids = _seed_star_wish_versions(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "银河星愿怎么样"},
    )
    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "我不买星愿了，想要15万的燃油车，家用，平时2个人坐"},
    ).json()
    assert rec["filters"]["locked_series_ids"] == [], "否定锁定车系本身等同明确解锁"
    assert rec["recommended_variants"], "解锁后应给出全市场候选，而不是空结果"
    series_ids = {v["series_id"] for v in rec["recommended_variants"]}
    assert ids["corolla"] in series_ids and ids["star"] not in series_ids


def test_negates_series_targets_series_only():
    """否定词必须**近距离指向车系名**才算否定该车系（评审 m2 的判定边界）。"""
    assert negates_series("我不买星愿了，想要15万的燃油车", "星愿")
    assert negates_series("不考虑银河星愿", "星愿")
    assert negates_series("排除星愿", "星愿")
    assert not negates_series("不想要SUV了，就看看银河星愿", "星愿"), "否定的是车身形式"
    assert not negates_series("不要纯电了，看看星愿", "星愿"), "否定的是能源类型"
    assert not negates_series("我想看看星愿", "星愿")
    assert not negates_series("我不买汉兰达", "星愿")


def test_negation_drops_only_the_negated_series(client: TestClient, db_session: Session):
    """评审 m18：否定只解除被点名否定的车系，同句提到的其他锁定车系保留。"""
    ids = _seed_star_wish_versions(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "我在看银河星愿和丰田卡罗拉"},
    )
    # 双车系被点名 → 车系对比问答；否定词只指向卡罗拉，星愿的锁定必须保留
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "我不买卡罗拉了，再看看银河星愿"},
    )
    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "预算10万以内，纯电，通勤用，2个人"},
    ).json()
    assert rec["filters"]["locked_series_ids"] == [ids["star"]], "未被否定的星愿应保持锁定"
    assert {v["series_id"] for v in rec["recommended_variants"]} == {ids["star"]}


def test_negation_about_other_dimension_keeps_lock(client: TestClient, db_session: Session):
    """反向保护：句中有否定词但不是否定该车系时，不得把用户点名的车系解掉。

    用「不买进口的」而不是「不要…/不考虑…」：extract_hints 对后两者会把句内
    所有能源/车身词一并写入 avoid（评审 L2 的既有口径），那样「纯电」会被避开，
    本用例就测不到锁定了。
    """
    ids = _seed_star_wish_versions(db_session)
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "我在看银河星愿"},
    )
    rec = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        json={"message": "不买进口的，就看看银河星愿，预算10万，纯电，通勤用，2个人"},
    ).json()
    assert rec["filters"]["locked_series_ids"] == [ids["star"]], "点名车系应保留锁定"
    assert {v["series_id"] for v in rec["recommended_variants"]} == {ids["star"]}
    assert not any("已解除对" in r for r in rec["reasons"]), "未冲突时不应出现解锁说明"
