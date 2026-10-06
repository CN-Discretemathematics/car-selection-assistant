"""裸品牌作为比较候选时的回归：必须查库反问，且**不能吞掉已答对的那台车**（2026-10-06）。

用户 2026-10-06 拍板的方案 B。真实问句：

    用户：大众和汉哪个好
    库里：大众有 29 款在售，它不是一款车；汉在库里且参数完整
    修好的行为：先正常回答汉，再在**末尾追加**一段说明

两个关键点，缺一个都不算修好：

1. **追加，不是替换**。第一版做成替换，朗逸的 807 字参数卡被整段丢掉，只剩一句
   反问——用户点名两台，一台的数据也没了。所以断言必须包含「库里有的那台车
   仍然出现在回答里」，而不能只检查反问文案。
2. **查库判定，不靠字符串猜**。`brands` 与 `vehicle_series` 是两张表，查一下就知道
   「大众」有几款在售。四轮审查反复翻车的那类启发式（品牌前缀判断、子串排除、
   连接词计数）这里一个都不需要。

此前这条用例只检查一句具体文案（「我们库里目前**没有**」不出现）——文案一改它就
查不出问题，那正是它失效的原因。现断言**行为**：报出车系数量、出现反问、给出可点
的车系样例，且已答对的那台车仍在。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.series_qa import build_series_qa_answer
from app.catalog.series_index import resolve_series
from tests.seed import (
    make_brand,
    make_series,
    make_source,
    make_variant,
    make_year,
)


def _seed(db: Session):
    """种子库刻意造成「裸品牌 + 库里有的车系」并存的形态。"""
    src = make_source(db, name="汽车之家")
    m6_brand = make_brand(db, name="问界", source=src)
    gq = make_brand(db, name="传祺", source=src)          # 库内有这个品牌
    jl = make_brand(db, name="吉利", source=src)
    vw = make_brand(db, name="大众", source=src)
    sk = make_brand(db, name="斯柯达", source=src)
    jiem6 = make_series(db, m6_brand, name="问界M6", body_type="suv",
                        energy_types=("REEV",), source=src)
    make_series(db, gq, name="传祺GS4", body_type="suv", energy_types=("ICE",), source=src)
    make_series(db, jl, name="几何M6", body_type="suv", energy_types=("BEV",), source=src)
    langyi = make_series(db, vw, name="朗逸", body_type="sedan",
                         energy_types=("ICE",), source=src)
    make_series(db, sk, name="柯珞克", body_type="wagon", energy_types=("ICE",), source=src)
    for s in (jiem6, langyi):
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="230000", source=src,
                     facts=[("参数信息", "CLTC纯电续航里程(km)", "605", "km", "CLTC")])
    db.commit()
    return jiem6, langyi


def test_bare_brand_ask_lists_its_series(db_session: Session):
    """裸品牌作为比较候选时，必须**查库列出该品牌的车系并反问**（用户 2026-10-06 拍板方案 B）。

    「大众和汉哪个好」——大众有 29 款在售，它不是一款车。此前系统答
    「我只查到 1 台（汉）」，把汉的数据也一起藏起来：用户点名两台，一台数据没有。

    判据是**查库**（`brands` 表），不是字符串猜测——四轮审查反复翻车的那类
    启发式（品牌前缀判断、子串排除、连接词计数）在这里一个都不需要。

    断言**行为**而非文案：库里有的那台车必须仍然出现在回答里，且必须出现
    车系数量与反问。此前这条测试只检查一句具体文案（「我们库里目前**没有**」
    不出现），文案一改它就查不出问题——那正是它失效的原因。
    """
    _seed(db_session)
    # 种子库：问界M6 / 传祺GS4 / 几何M6 / 朗逸(大众) / 柯珞克(斯柯达)
    for msg, brand_label, expected_names in (
        ("传祺和朗逸哪个好", "传祺", ["传祺GS4"]),
        ("大众柯珞克哪个好", "大众", ["朗逸"]),
    ):
        resolved = resolve_series(db_session, msg)
        text = build_series_qa_answer(db_session, resolved, msg)
        assert "我们库里目前**没有**" not in text, (
            f"「{msg}」把在库品牌说成库里没有。实际：{text[:160]}"
        )
        assert brand_label in text and "款在售车" in text, (
            f"「{msg}」应报出「{brand_label}」的车系数量。实际：{text[:160]}"
        )
        assert "想比哪一款" in text, f"应反问用户想比哪一款。实际：{text[:160]}"
        for name in expected_names:
            assert name in text, (
                f"「{msg}」应给出可点的车系样例 {name}。实际：{text[:160]}"
            )

    # 查得到的那台车的数据不能被这段反问吞掉
    msg = "传祺和朗逸哪个好"
    resolved = resolve_series(db_session, msg)
    text = build_series_qa_answer(db_session, resolved, msg)
    assert "朗逸" in text, f"朗逸在库里，回答里必须出现它。实际：{text[:160]}"
