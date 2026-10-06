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


# ─────────────────────────────────────────────────────────────────────────────
# 2026-10-06 独立审查（判不通过）后补的回归。
#
# 上一批用例的两个问句「传祺和朗逸哪个好」「大众柯珞克哪个好」都只解析出**1**个
# 车系，只走单车系分支。三处调用点里另外两处**零覆盖**——独立审查给
# `_brand_disclosure` 装探针跑完整套件，非空返回只出现 3 次且全是 `len==1`。
# 下面这批专门补上双车系 / 三车系，以及「不该反问」的两种反向情形。
# ─────────────────────────────────────────────────────────────────────────────


def _seed_multis(db: Session):
    """多种子：覆盖同级/异级/三车系，以及两种**不该**反问的形态。"""
    src = make_source(db, name="汽车之家")
    vw = make_brand(db, name="大众", source=src)
    gq = make_brand(db, name="传祺", source=src)
    sk = make_brand(db, name="斯柯达", source=src)
    jiguang = make_brand(db, name="极光", source=src)   # 库里有车系 → 排除条件不成立
    # 启辰只通过问句文本被提到（「启辰和朗逸哪个好」），不需要持有引用
    make_brand(db, name="启辰", source=src)             # 库里 0 款在售
    langyi = make_series(db, vw, name="朗逸", source=src, positioning="紧凑型车")
    xuanyi = make_series(db, vw, name="轩逸", source=src, positioning="紧凑型车")
    maiteng = make_series(db, vw, name="迈腾", source=src, positioning="中型车")
    # 品牌名「极光」嵌在**别的**品牌（斯柯达）的车系名里 —— 与真实库的
    # 「AMG GT」含「MG」、「宏光MINIEV」含「MINI」同构。
    make_series(db, sk, name="极光星舰", source=src, positioning="中型SUV")
    make_series(db, gq, name="传祺GS4", source=src, positioning="紧凑型SUV")
    make_series(db, jiguang, name="极光007", source=src, positioning="轿车")
    for s in (langyi, xuanyi, maiteng):
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db.commit()


def _ask(db: Session, msg: str):
    resolved = resolve_series(db, msg)
    return resolved, build_series_qa_answer(db, resolved, msg)


def test_two_series_same_class_still_gets_brand_ask(db_session: Session):
    """两款车**同级**时品牌反问也必须追加——独立审查 S1 抓到的那处缩进。

    此前这三行写在 `if same_class: ... else: ...` 的 `else` 里，于是
    `same_class` 为真时整段被跳过：「传祺和朗逸、轩逸哪个好」里「传祺」是个
    品牌，系统却一声不吭。

    现有测试抓不到，是因为 `make_series()` 从不设 `positioning`，种子车系全为
    `None` → `same_class` 恒假 → 这条分支走不到。种子加了 `positioning` 才走得进去。
    """
    _seed_multis(db_session)
    resolved, text = _ask(db_session, "传祺和朗逸、轩逸哪个好")
    assert len(resolved) == 2, f"这条用例的前提是解析出 2 台，实际 {[s.name for s, _ in resolved]}"
    assert resolved[0][0].positioning == resolved[1][0].positioning == "紧凑型车", (
        "这条用例的前提是两台同级，实际 "
        f"{[s.positioning for s, _ in resolved]}"
    )
    assert "款在售车" in text and "想比哪一款" in text, (
        f"同级双车系时品牌反问被整段丢弃了。实际末尾：{text[-160:]}"
    )
    for name in ("朗逸", "轩逸"):
        assert name in text, f"「{name}」在库里，回答里必须出现它。实际末尾：{text[-160:]}"


def test_two_series_different_class_gets_brand_ask(db_session: Session):
    """异级双车系同样要追加——与上一条对照，防止只修好其中一支。"""
    _seed_multis(db_session)
    resolved, text = _ask(db_session, "传祺和朗逸、迈腾哪个好")
    assert len(resolved) == 2, f"实际 {[s.name for s, _ in resolved]}"
    assert resolved[0][0].positioning != resolved[1][0].positioning, "前提是两台异级"
    assert "款在售车" in text and "想比哪一款" in text, (
        f"异级双车系时没有品牌反问。实际末尾：{text[-160:]}"
    )


def test_three_series_gets_brand_ask(db_session: Session):
    """≥3 车系的汇总路径也要追加。"""
    _seed_multis(db_session)
    resolved, text = _ask(db_session, "传祺和朗逸、轩逸、迈腾哪个好")
    assert len(resolved) == 3, f"实际 {[s.name for s, _ in resolved]}"
    assert "款在售车" in text and "想比哪一款" in text, (
        f"三车系汇总时没有品牌反问。实际末尾：{text[-200:]}"
    )
    for name in ("朗逸", "轩逸", "迈腾"):
        assert name in text, f"「{name}」被汇总路径吞掉了。实际末尾：{text[-200:]}"


def test_brand_word_inside_resolved_series_name_is_not_a_brand_ask(db_session: Session):
    """品牌名嵌在**别的**品牌的车系名里时，不能判成「用户在问品牌」（独立审查 S2）。

    真实库原文（修复前）：

        「AMG GT 值得买吗」    → 解析出 AMG GT，却被判成在问品牌「MG」，
                                报「库里有 0 款」并推荐朗逸；
        「宏光MINIEV 值得买吗」→ 同理，推荐电动 MINI；
        「东风本田S7 值得买吗」→ 「东风」当成品牌，推荐御风EM27。

    本用例用「极光星舰」（斯柯达）含品牌「极光」同构构造，且**刻意让极光在库
    里有车系**——这样即使位置判定整个失效、只剩「0 款不反问」那道，它也会暴露。
    """
    _seed_multis(db_session)
    resolved, text = _ask(db_session, "极光星舰值得买吗")
    assert len(resolved) == 1 and resolved[0][0].name == "极光星舰", (
        f"前提是解析出斯柯达极光星舰，实际 {[s.name for s, _ in resolved]}"
    )
    assert "是品牌" not in text, (
        f"「极光」嵌在车系名「极光星舰」里，不该被判成在问品牌。实际末尾：{text[-200:]}"
    )
    assert "想比哪一款" not in text, f"不该出现品牌反问。实际末尾：{text[-200:]}"


def test_brand_with_zero_active_series_is_not_disclosed(db_session: Session):
    """库里 0 款在售的品牌不反问（用户 2026-10-06 拍板）。

    「库里有 0 款在售车……（比如朗逸）」是自相矛盾的话。此前 `_sample_series_names`
    还会硬编码兜底成「朗逸」，两处叠加就成了这种句子。
    """
    _seed_multis(db_session)
    resolved, text = _ask(db_session, "启辰和朗逸哪个好")
    assert len(resolved) == 1 and resolved[0][0].name == "朗逸", (
        f"前提是只解析出朗逸，实际 {[s.name for s, _ in resolved]}"
    )
    assert "启辰" not in text, f"启辰库里 0 款在售，不该出现在回答里。实际末尾：{text[-200:]}"
    assert "0 款" not in text, f"不该报「0 款在售车」。实际末尾：{text[-200:]}"
    assert "朗逸" in text, "朗逸在库里且是已答对的那台，不能被吞掉"


def test_brand_of_the_resolved_series_is_not_disclosed(db_session: Session):
    """品牌与**自家**车系分开写时也不反问——用户已经点名到车系了，再问一遍是废话。

    真实库实测「大众和朗逸哪个好」：朗逸就是大众的车，系统只正常答朗逸（312 字），
    不追加「大众是品牌……想比哪一款？比如**朗逸**」——那等于把用户刚给的车系名
    原样推荐回去。

    挡住它的是 `resolved_brands` 相减（朗逸的品牌就是大众），不是位置判断；
    这条用例把这个区别钉住，防止以后重构时只保留位置判断而丢掉相减。
    """
    _seed_multis(db_session)
    resolved, text = _ask(db_session, "大众和朗逸哪个好")
    assert len(resolved) == 1 and resolved[0][0].name == "朗逸", (
        f"前提是只解析出朗逸，实际 {[s.name for s, _ in resolved]}"
    )
    assert "想比哪一款" not in text, (
        f"朗逸本身就是大众的车，不该再反问「大众是品牌」。实际末尾：{text[-200:]}"
    )
    assert "朗逸" in text, "朗逸是已答对的那台车，必须仍然出现"


def test_brand_word_appearing_both_inside_and_outside_is_disclosed(db_session: Session):
    """品牌词**既在车系名内部、又在别处独立出现**时，独立的那次要算数。

    这是位置判定与早先子串排除**真正不等价**的地方。真实库实测五类问句
    （`北京现代ix35和北京`、`长安启源A06和长安`、`长城猛龙 PLUS和长城`、
    `郑州日产Z9 GE PHEV和日产`、`东风风神E70和东风`）：位置判定反问，子串排除全不反问。

    子串排除是全局一刀切——`any(name in s for s in owned)` 命中一次就把整个品牌词
    排掉，连独立那次一起排。后果是用户提了「北京」而系统对「北京」一个字不提，
    **而全量测试照样全绿**。这条用例就是防这个的。
    """
    src = make_source(db_session, name="汽车之家")
    bj_modern = make_brand(db_session, name="北京现代", source=src)
    bj = make_brand(db_session, name="北京", source=src)
    s = make_series(db_session, bj_modern, name="现代ix35", source=src, positioning="紧凑型SUV")
    make_series(db_session, bj, name="北京越野BJ40", source=src, positioning="中型SUV")
    db_session.commit()

    resolved, text = _ask(db_session, "北京现代ix35和北京哪个好")
    assert len(resolved) == 1 and resolved[0][0].name == "现代ix35", (
        f"前提是只解析出北京现代ix35，实际 {[s.name for s, _ in resolved]}"
    )
    assert "款在售车" in text and "想比哪一款" in text, (
        f"「北京」在「北京现代ix35」之外独立出现了一次，必须反问。实际末尾：{text[-200:]}"
    )
    assert s.name in text or "ix35" in text, "已答对的那台车不能被吞掉"


def test_phantom_brand_row_falls_back_to_series_name_prefix(db_session: Session):
    """空的重复品牌行要靠「车系名以该品牌词开头」兜住，否则用户点名的品牌被整段吞掉。

    真实库事实（独立审查实测）：

        「MG」  → 该 brand_id 下 0 款在售
        「名爵」→ 该 brand_id 下 7 款：MG4 / MG5 / MG6 / MG7 / MG 4X / MG ES5 / MG Cyberster

    只按 `brand_id` 数，「MG 和汉哪个好」算出的 MG 是 0 款，被「0 款不反问」
    那条整段丢弃——**回答 334 字，里面一个字都不提 MG**。而用户明明点名了它。

    这里用同构数据构造：品牌行「极氪」下 0 款在售，车系「极氪001」挂在另一个
    品牌行「吉利汽车」下。兜底只对**算出来是 0** 的品牌生效，所以「吉利汽车」
    自己的计数不受影响。
    """
    src = make_source(db_session, name="汽车之家")
    geely = make_brand(db_session, name="吉利汽车", source=src)
    make_brand(db_session, name="极氪", source=src)      # 空行：下面不放车系
    make_series(db_session, geely, name="极氪001", source=src, positioning="中型轿车")
    vw = make_brand(db_session, name="大众", source=src)
    langyi = make_series(db_session, vw, name="朗逸", source=src, positioning="紧凑型车")
    year = make_year(db_session, langyi)
    make_variant(db_session, langyi, year, config_version="旗舰", energy_type="BEV",
                 price_cny="150000", source=src)
    db_session.commit()

    resolved, text = _ask(db_session, "极氪和朗逸哪个好")
    assert len(resolved) == 1 and resolved[0][0].name == "朗逸", (
        f"前提是只解析出朗逸（极氪不是车系名），实际 {[x.name for x, _ in resolved]}"
    )
    assert "极氪" in text and "款在售车" in text, (
        f"「极氪」独立出现、且车系挂在另一个品牌行下，不该被整段吞掉。实际末尾：{text[-200:]}"
    )
    assert "朗逸" in text, "已答对的那台车不能被吞掉"
