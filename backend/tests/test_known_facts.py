"""P0-3 假否定守卫：库里明明有动力参数，系统却说「我手头没有」。

生产实测（2026-10-05）：用户点完卡片追问「动力」，系统回复
「我手头暂时没有捷途旅行者C-DM、方程豹钛7这几款20万内车型的动力参数」。
实测生产库：捷途旅行者C-DM `最大功率(kW)=280`、`最大扭矩(N·m)=610`。

本组钉住两件事：
1. `build_known_facts` 能把这些事实从库里取出来（证据前置）。
2. `false_denial_hits` 认得出身处那句「没有」，且**不误伤合法否定**。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.known_facts import build_known_facts, false_denial_hits
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

# 生产库里真实存在的原文（站点实拍）
PROD_DENIAL = (
    "收到，您比较关注动力。不过很抱歉，我手头暂时没有捷途旅行者C-DM、"
    "方程豹钛7这几款20万内车型的动力参数，不能凭空给您说。"
)


def _seed(db: Session) -> list[int]:
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="捷途", source=source)
    s = make_series(db, brand, name="捷途旅行者C-DM", body_type="suv",
                    energy_types=("PHEV",), source=source)
    y = make_year(db, s)
    v = make_variant(
        db, s, y, price_cny="197900", energy_type="PHEV",
        facts=[
            ("参数信息", "最大功率(kW)", "280", "kW", None),
            ("参数信息", "最大扭矩(N·m)", "610", "N·m", None),
            ("参数信息", "座位数(个)", "5", "个", None),
        ],
        source=source,
    )
    db.commit()
    return [v.id]


# ── 证据前置 ────────────────────────────────────────────────────────────────
def test_build_known_facts_reads_power_from_db(db_session: Session):
    ids = _seed(db_session)
    block, dims = build_known_facts(db_session, ids)
    assert "280" in block and "610" in block, "功率/扭矩必须原样出现在上下文块里"
    assert "power" in dims
    # 块必须自称权威，否则模型仍会按「检索片段」对待它
    assert "权威" in block


def test_build_known_facts_empty_ids_returns_nothing(db_session: Session):
    block, dims = build_known_facts(db_session, [])
    assert block == "" and dims == set()


def test_build_known_facts_no_facts_no_dimensions(db_session: Session):
    """没有事实时不得凭空声称某个维度有数据——否则守卫会拦下合法的「未披露」。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试", source=source)
    s = make_series(db_session, brand, name="无参车", body_type="suv",
                    energy_types=("BEV",), source=source)
    y = make_year(db_session, s)
    v = make_variant(db_session, s, y, price_cny="100000", energy_type="BEV", source=source)
    db_session.commit()
    block, dims = build_known_facts(db_session, [v.id])
    assert dims == set()
    assert "最大功率" not in block


# ── 守卫：认得出 ────────────────────────────────────────────────────────────
def test_false_denial_catches_prod_sentence():
    """生产实拍的原文必须被抓住——这是本次修复的全部起因。"""
    assert "power" in false_denial_hits(PROD_DENIAL, {"power"})


def test_false_denial_ignores_dimension_without_data():
    """**没有**数据的维度说未披露是完全正确的，不能拦。"""
    assert false_denial_hits(PROD_DENIAL, {"space"}) == []


def test_false_denial_ignores_normal_answer():
    """正常引用数据作答不该被拦。"""
    text = "这几款最大功率分别是 280kW 和 335kW，扭矩也都够家用。"
    assert false_denial_hits(text, {"power"}) == []


# ── 守卫：不误伤（这类守卫最危险的失败模式）────────────────────────────────
def test_false_denial_allows_legitimate_negation_about_a_specific_version():
    """「没有 7 座版本」是合法陈述；座位本就不在守卫维度里，且无数据线索。"""
    text = "这几款里没有 7 座的版本，最高的就是 5 座。"
    assert false_denial_hits(text, {"power", "space"}) == []


def test_false_denial_allows_negation_about_other_brand():
    """否定句落在别的品牌上、且不涉及被问维度 → 不拦。"""
    text = "没有比亚迪的数据我不敢乱说，不过捷途这台动力参数是够的。"
    assert "power" not in false_denial_hits(text, {"power"})


def test_false_denial_empty_inputs():
    assert false_denial_hits("", {"power"}) == []
    assert false_denial_hits(PROD_DENIAL, set()) == []


def test_false_denial_attributes_to_nearest_dimension_only():
    """一句里两个维度词时只判离否认词最近的那个——否则会连坐、误改正确答案。"""
    text = "动力参数都披露了，但续航我没有数据。"
    assert false_denial_hits(text, {"power", "range"}) == ["range"]


def test_false_denial_catches_denial_far_from_dimension_word():
    """否认词与维度词之间可以隔着一整串车系名——守卫不能靠固定字符窗口。"""
    text = (
        "很抱歉，我手头暂时没有捷途旅行者C-DM、方程豹钛7这两款20万内车型的动力参数，"
        "不能凭空给您说。"
    )
    assert "power" in false_denial_hits(text, {"power"})
