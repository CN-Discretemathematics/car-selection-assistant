"""真实用户问句下的「不得静默替换车系」回归（2026-10-06）。

语料来源：**懂车帝车友圈「提问」帖原文 + 汽车之家问答区标题**——都是用户真敲的
问句。半编辑的论坛/自媒体标题**不收**：措辞太规整，会把基线测得比真实情况好看。

## 这批语料为什么必须用真实的

之前用「拿库里名字去注入模板」的自造模板测出 **100% 召回**，等于自己给自己
递答案。真实语料一上来就打破这个前提：

    用户：传祺M6值得买吗?
    库里：**没有传祺M6**（传祺有 ES9 / E8 / 影豹 / GS3 / GS4 / GS8）
    实际：把「M6」解析成 **问界M6**，输出了问界 M6 的尺寸/轴距/动力/续航/油耗

数据全是真的，只是车不是用户问的那台。这比崩溃更坏：崩溃用户会重试，
自信的错答案用户会照着买。

    用户：大众朗逸和明锐哪个更好
    实际：只解析出朗逸；明锐（库里没有）一个字都没提，
          转而去问「可以告诉我你的预算吗？」——而用户明明点了名。

## 三条底线（逐条钉住）

1. **绝不用名字相近的另一台车替代**。用户点名的品牌与作答车系品牌不符时，
   必须说「库里没有」。
2. **点名多台只查到一部分时必须说清楚**，不能让用户以为看全了。
3. **点名了车就必须回答**，不许掉进推荐链去追问已经给过的信息。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.routing import decide_route
from app.agent.schemas import UserProfile
from app.agent.series_qa import build_series_qa_answer
from app.catalog.series_index import resolve_series
from tests.seed import (
    make_brand,
    make_series,
    make_source,
    make_variant,
    make_year,
)

# ── 真实问句（逐字保留原始标点与错别字）────────────────────────────────
REAL_QUERIES = [
    # 懂车帝「提问」帖
    "传祺M6值得买吗?",
    "大众朗逸和明锐哪个更好",
    "奥迪A3和奔驰A180哪个更值得选",
    "别克新英朗和卡罗拉谁更值得选",
    "标志4008和本田CR-V谁更值得选",
    "丰田RV4和本田CR-V哪个更值得选",
    "凯迪拉克ATS和宝马3系哪个更值得选",
    "大众Polo与本田飞度,谁更值得选择?",
    "新英朗和308哪个更值得选",
    "长安新能源奔奔和奇瑞新能源EQ哪个更值得选",
    "什么车销量最好",
    "有没有推荐的车型呀,价格12w左右,然后空间大一点的,电混,新手小白不知道怎么选",
    "友友们,30w油车suv求推荐",
    "是买悦也,还是买icar03,还是买缤果……目前存款四万,全款买车不",
    "想问一下,工资四五千,平时上班距离18km左右(需要驾车),女生,城市代步,"
    "适合开哪款电车?目前看了海豹、极氪007、小米su7",
    "看上了银河l6,第一辆车,新手,有人指导下么",
    # 汽车之家问答区标题
    "奥迪A3与奔驰GLA,谁更值得入手?",
    "奥迪A3与速腾,谁更值得入手?",
    "奥迪A3与新思域,谁更值得入手?",
    "途乐车内噪音表现如何?",
    "汉兰达与雷克萨斯RX哪个更值得选",
]


def _seed(db: Session):
    """刻意造出「同名不同牌」与「一半在库一半不在」两种真实形态。"""
    src = make_source(db, name="汽车之家")
    m6_brand = make_brand(db, name="问界", source=src)
    gq = make_brand(db, name="传祺", source=src)          # 库内有这个品牌，但没有 M6
    jl = make_brand(db, name="吉利", source=src)
    vw = make_brand(db, name="大众", source=src)
    sk = make_brand(db, name="斯柯达", source=src)        # 库内有品牌，也没有明锐
    # 与「传祺M6」的「M6」同名不同牌 —— 用来复现静默替换
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


def test_absent_car_is_never_silently_substituted(db_session: Session):
    """底线①：用户点名的品牌与作答车系不符 → 视为「库里没有」，绝不用另一台顶替。

    判据是**品牌对不上**：它不需要知道传祺M6 长什么样，只需要知道答案不该是问界的。
    断言的是**最终回答文本**——只测解析结果的话，修复没接上也会绿。
    """
    jiem6, _ = _seed(db_session)
    msg = "传祺M6值得买吗?"
    resolved = resolve_series(db_session, msg)
    assert any(s.id == jiem6.id for s, _ in resolved), (
        "前提不成立：若根本不会误命中问界M6，这条用例就测不到替换问题"
    )
    text = build_series_qa_answer(db_session, resolved, msg)
    assert "问界M6" not in text, (
        f"回答里仍出现问界M6 —— 用户的传祺M6 被静默替换成另一台车。实际：{text[:200]}"
    )
    assert "605" not in text, (
        f"问界M6 的参数不该作为传祺M6 的答案发出去。实际：{text[:200]}"
    )
    assert "传祺" in text and "没有" in text, (
        f"应当明说「传祺那台我们库里没有」。实际：{text[:200]}"
    )


def test_partial_multi_car_query_must_disclose_the_missing_one(db_session: Session):
    """底线②+③：点名两台只查到一台 → 走到车系问答，并披露漏了。"""
    _seed(db_session)
    msg = "大众朗逸和明锐哪个更好"
    resolved = resolve_series(db_session, msg)
    names = [s.name for s, _ in resolved]
    assert "朗逸" in names, "前提不成立：朗逸应当能解析出来"
    assert "明锐" not in names, "前提不成立：明锐不在库里，不应被解析出来"

    decision = decide_route(msg, {}, True, UserProfile(), resolved, db_session)
    assert decision.intent == "series_qa", (
        f"点名了朗逸却落到 {decision.intent}（{decision.matched_rule}）——"
        "原先「哪个更好」不在触发词表，会去追问用户已经给过的信息"
    )
    text = build_series_qa_answer(db_session, resolved, msg)
    assert "没有收录" in text or "库里没有" in text, (
        f"应当披露「明锐我们库里没有」。实际：{text[:240]}"
    )
    assert "朗逸" in text, "查得到的那台仍要正常回答，不能因为漏了一台就什么都不答"


def test_present_car_is_not_falsely_disclaimed(db_session: Session):
    """反向保护：库里**有**的车不能被误判成「没有」。

    披露逻辑的最大风险是把正常问题也推给「库里没有」——那就从修 bug 变成了制造 bug。
    """
    _seed(db_session)
    msg = "问界M6的续航是多少"
    resolved = resolve_series(db_session, msg)
    text = build_series_qa_answer(db_session, resolved, msg)
    assert "库里" not in text or "没有收录" not in text, (
        f"问界M6 明明在库里，不该出现「没有收录」的披露。实际：{text[:200]}"
    )
    assert "605" in text, f"应当正常报出参数。实际：{text[:200]}"


@pytest.mark.parametrize("msg", REAL_QUERIES)
def test_real_queries_never_drop_a_named_car(db_session: Session, msg: str):
    """底线③：点名了车系却落到推荐/追问，等于没回答用户的问题。

    这条不要求「一定答对」，只要求**不许装作没听见车名**。
    """
    _seed(db_session)
    resolved = resolve_series(db_session, msg)
    if not resolved:
        pytest.skip("该问句未解析出车系（库里缺车），由专门的披露用例覆盖")
    decision = decide_route(msg, {}, True, UserProfile(), resolved, db_session)
    assert decision.intent not in ("recommendation", "chitchat"), (
        f"「{msg}」点名了 {[s.name for s, _ in resolved]}，"
        f"却落到 {decision.intent}（{decision.matched_rule}）"
    )


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("大众朗逸和明锐哪个更好", 2),
        ("买A或者B", 2),          # 「或者」含「或」：逐项 count 会数成 3
        ("A或B", 2),              # 只留「或者」不数「或」的话会少算成 1
        ("买A还是B", 2),
        ("A vs B", 2),
        ("A和B或者C和D", 4),
        ("目前看了海豹、极氪007、小米su7", 3),   # 真实问句里的「、」枚举
        ("我的车和朋友的以及公司的", 3),
        ("汉怎么样", 1),
        ("汉的续航是多少", 1),
        ("预算20万，家用5口人，想要新能源SUV", 1),
        # 逗号**不能**计入：它在正常句子里只是停顿，计入会把每个逗号句
        # 都当成并列而误报缺车
        ("汉的续航是多少，油耗呢", 1),
    ],
)
def test_candidate_count_uses_longest_first_alternation(
    msg: str, expected: int
):
    """候选台数估算必须**用 findall 整体匹配**，不能逐项 `count()` 相加。

    2026-10-06 自查抓到：连接词表里同时有 `或` 与 `或者`，求和让「买A或者B」
    数成 3 台 → 正常的两车问题**误报缺车**（披露逻辑自己的误报，比漏报更糟）。

    ⚠️ 函数名里的「longest-first」是**历史遗留的错名**，已更正过一次：
    当时我以为交替顺序必须是「最长优先」，变异测试把 `或` 排到 `或者` 前面，
    17 条全绿——因为计数场景下顺序根本不影响结果（「或者」无论被哪个分支吃掉
    都只产生 1 个匹配）。真正的关键只是「用 findall」。改名会牵动本轮多处引用，
    暂留旧名但在此说明，**别被这个名字误导**。
    """
    from app.agent.series_qa import _implied_candidate_count

    assert _implied_candidate_count(msg) == expected


def test_bare_brand_mention_is_not_a_missing_car(db_session: Session):
    """P0 回归（2026-10-06 独立审查）：**裸品牌名**不得被判成「库里没有」。

    「大众和汉哪个好」——大众有 29 款在售车，它不是一款车，更不是「没收录」。
    披露逻辑最初只判「消息里出现了库内品牌 A，而解析出的车系属品牌 B」，
    于是把 **39 个在库品牌**（奔驰 56 款 / 大众 29 / 丰田 27…）判成
    「我们库里目前没有对应的车系资料」并**替换掉整段回答**——
    比它要修的缺陷更严重，且更难被察觉（用户只会以为这台车没数据）。

    区分：品牌词**紧跟字母数字**才是「点名了某品牌下的一台车」（传祺M6）；
    独立出现只是比较候选方之一（大众**和**汉）。
    """
    _seed(db_session)
    # 本文件的种子库里有：问界M6 / 传祺GS4 / 几何M6 / 朗逸(大众) / 柯珞克(斯柯达)
    for msg in ("传祺和朗逸哪个好", "大众柯珞克哪个好"):
        resolved = resolve_series(db_session, msg)
        text = build_series_qa_answer(db_session, resolved, msg)
        assert "我们库里目前**没有**" not in text, (
            f"「{msg}」把在库品牌说成库里没有。实际：{text[:160]}"
        )


def test_attribute_conjunction_is_not_multi_car(db_session: Session):
    """P1 回归（2026-10-06 独立审查）：「油耗**和**续航」是一台车的两个属性。

    曾被算成「你问的 2 台里我们只查到 1 台」，**一个参数都不给**。
    触发条件是没有显式比较信号时仍拿连接词去猜候选台数。

    「哪个重要」也算属性比较——故 `哪个` 必须带选车类后缀（好/强/值得/划算…）
    才算比较信号，这一点也一并钉住。
    """
    _seed(db_session)
    for msg in ("朗逸的油耗和续航分别是多少", "朗逸的油耗和续航哪个重要"):
        resolved = resolve_series(db_session, msg)
        text = build_series_qa_answer(db_session, resolved, msg)
        assert "你问的「" not in text and "没有收录" not in text, (
            f"「{msg}」是属性并列，不该按多车处理。实际：{text[:160]}"
        )
        assert "续航" in text or "参数" in text or "朗逸" in text, (
            f"「{msg}」应当正常给参数。实际：{text[:160]}"
        )


def test_real_comparison_still_discloses_missing_car(db_session: Session):
    """反向保护：上面两条修完后，**真正的缺车对比仍要披露**。

    「大众朗逸和明锐哪个更好」：朗逸在库、明锐不在。
    注意「大众」是「大众朗逸」的组成部分，不得被当成独立候选品牌而吞掉披露。
    """
    from app.agent.series_qa import build_series_qa_answer as _build

    _seed(db_session)
    msg = "大众朗逸和明锐哪个更好"
    resolved = resolve_series(db_session, msg)
    text = _build(db_session, resolved, msg)
    assert "没有收录" in text or "库里没有" in text, (
        f"明锐确实不在库里，应当披露。实际：{text[:200]}"
    )
    assert "朗逸" in text, "查得到的那台仍要正常回答"


@pytest.mark.xfail(
    reason="销量榜正则漏接真实主句式（「什么车销量最好」等 11 条已实测漏接）；"
           "属口径变更，待产品拍板后修——见 docs 台账",
    strict=False,
)
def test_sales_wording_from_real_users_is_not_missed(db_session: Session):
    """真实主句式「什么车销量最好」应进销量榜分支。

    我此前自造模板写的是「什么车卖得好」——恰好绕开了漏接的句式，
    于是自测 100% 通过、真实语料直接掉出来。**真实语料才是门禁。**
    """
    _seed(db_session)
    for msg in ("什么车销量最好", "什么车卖得好", "销量前十", "什么车热销"):
        decision = decide_route(msg, {}, True, UserProfile(), [], db_session)
        assert decision.intent == "sales_ranking", (
            f"「{msg}」应落销量榜，实际 {decision.intent}（{decision.matched_rule}）"
        )
