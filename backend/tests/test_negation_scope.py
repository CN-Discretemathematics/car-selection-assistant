"""否定句判定：从「全句级」改为「逐关键词 + 分句 + 邻近 + 的字边界」（2026-10-03）。

## 为什么这组测试值钱

判定曾经是 `if "不要" in message or "不考虑" in message:`——只要句子里出现否定词，
**句中所有**车型/能源关键词一律进 `avoid`。两个已确认的缺陷（提案 §4.3）：

1. 词表只 2 个词 →「不想开MPV」「不喜欢轿车」进不了否定分支，落进**偏好**路径，
   系统**推荐用户明确排斥的车**；
2. 无词边界的子串匹配 →「不要**太贵的**SUV」把 SUV 排除，而 `avoid` 直接下推
   SQL `NOT body_type IN (...)`，等于把用户唯一想要的车型从候选集删掉。

第 2 条比第 1 条更伤：它不是没听懂，是**反过来把用户要的东西删了**。

## 这组测试最容易被自己骗的地方

中文不做词边界就匹配，等于随机制造结论。写这份测试时本人先写了个审计脚本，
把「别」当否定词去扫语料，于是「差**别**在哪」「**别**克 E5」全被判成否定表达，
报出「7 条被误判」——**7 条全是假的**。所以「不产生 avoid」和「产生 avoid」
两类都必须显式测：只测后者的话，假朋友照样会悄悄溜过去。
"""
from __future__ import annotations

import pytest

from app.agent.engine import _negated_values, extract_hints


# ── 该排除的：否定直接管住关键词 ───────────────────────────────────────────
@pytest.mark.parametrize(
    ("message", "expect_avoid"),
    [
        ("不要MPV，预算20万", ["mpv"]),
        ("不考虑SUV", ["suv"]),
        ("不想开MPV", ["mpv"]),
        ("不喜欢轿车", ["sedan"]),
        ("不需要纯电车", ["BEV"]),
        ("别给我推MPV", ["mpv"]),
        ("不要燃油车", ["ICE"]),
        ("不考虑增程", ["EREV"]),
        # 同句多个关键词都在否定作用域内
        ("不要SUV和轿车", ["sedan", "suv"]),
    ],
)
def test_negation_excludes_the_named_body_type(message, expect_avoid):
    avoided = _negated_values(message.lower())
    assert avoided, f"「{message}」应当产生 avoid，实际为空"
    assert set(excluded_keyword_values(message, avoided)) == set(expect_avoid)


def excluded_keyword_values(message: str, avoided: list[str]) -> list[str]:
    """辅助：把 avoid 值映射回用户话里实际出现的词，便于断言可读。"""
    from app.agent.engine import _BODY_HINTS, _ENERGY_HINTS

    low = message.lower()
    hit = []
    for table in (_ENERGY_HINTS, _BODY_HINTS):
        for key, value in table.items():
            if key.lower() in low and value in avoided:
                hit.append(value)
    return hit


# ── 关键修复：「的」把否定与关键词隔开 → 关键词是**正向需求** ─────────────────
def test_negation_before_de_is_not_a_negation_of_the_keyword():
    """「不要太贵的SUV」= 要 SUV、但别太贵。旧实现把 SUV **排除**掉。

    这是本次修复里危害最大的一条：avoid 直接下推 SQL，会把用户唯一想要的车型删掉。
    """
    hints = extract_hints("不要太贵的SUV，预算20万")
    assert "avoid" not in hints, f"SUV 不该被排除：{hints}"
    assert hints.get("body_type") == ["suv"]


def test_de_guarded_keyword_stays_a_preference():
    """同一句里否定与正向要求并存时，正向要求必须保留。"""
    hints = extract_hints("不要MPV，想要SUV，预算20万")
    assert hints.get("avoid") == ["mpv"]
    assert hints.get("body_type") == ["suv"]


# ── 关键修复：词表太窄（「不想/不喜欢/不需要」旧实现不认）──────────────────
def test_narrow_vocabulary_is_no_longer_read_as_preference():
    """旧实现下这几句会写进 body_type（=推荐用户排斥的车），现在必须进 avoid。"""
    for message, value in (("不想开MPV", "mpv"), ("不喜欢轿车", "sedan"), ("不需要纯电车", "BEV")):
        hints = extract_hints(message)
        assert hints.get("avoid") == [value], f"「{message}」应排除 {value}：{hints}"
        # 关键：绝不能同时当成正向偏好（那才是原缺陷）
        assert value not in (hints.get("body_type") or []), f"「{message}」被当成了偏好：{hints}"
        assert value not in (hints.get("energy_preference") or []), f"「{message}」被当成了偏好：{hints}"


# ── 假朋友：中文不做词边界就匹配，随机制造结论 ─────────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        # ⚠️ 前两条刻意**不含「的」**：含「的」时「的」字边界已经挡住了，
        # 测不出假朋友过滤是否真的生效（反向验证实测：删掉该过滤仍全绿）。
        "这车和另一款的差别在MPV",
        "和同级的差别是轿车还是MPV",
        "差别在哪：坦克500 Hi4-T 和智享版",
        "推荐别克的SUV",
        "别克的MPV怎么样",
        "这台车和另一台的差别",
    ],
)
def test_false_friend_bae_ke_and_cha_bie_are_not_negations(message):
    """「差**别**」「**别**克」都不该触发否定。

    这条是本人审计脚本实测踩出来的：把「别」当否定词扫 520 条语料，报出 7 条
    「误判」，逐条核对后**全是假的**，全部来自「差别」「别克」。
    """
    assert _negated_values(message.lower()) == [], f"「{message}」被误判为否定"
    assert "avoid" not in extract_hints(message)


# ── 分句：否定作用域不跨标点 ───────────────────────────────────────────────
def test_negation_does_not_cross_clause_boundary():
    """「不喜欢轿车，SUV可以」= 只排除轿车，SUV 仍是候选。"""
    hints = extract_hints("不喜欢轿车，SUV可以，预算20万")
    assert hints.get("avoid") == ["sedan"]
    assert hints.get("body_type") == ["suv"]


def test_negation_after_a_comma_still_applies():
    """否定词在后半分句时同样生效。"""
    hints = extract_hints("预算20万，不要MPV")
    assert hints.get("avoid") == ["mpv"]


# ── 旧有的两条契约不能被这次重写破坏 ──────────────────────────────────────
def test_avoid_wins_over_preference_for_same_keyword():
    """同词同时命中偏好与避开时以避开为准（评审 L2 原有契约）。"""
    hints = extract_hints("不要燃油车")
    assert hints.get("avoid") == ["ICE"]
    assert "energy_preference" not in hints


def test_no_avoid_key_when_message_has_negation_but_no_vehicle_word():
    """含否定字样但没提车型时不得凭空造 avoid（评审 M-R2：否则 structured 误判）。"""
    hints = extract_hints("这个价格不要太高")
    assert "avoid" not in hints
    assert "body_type" not in hints and "energy_preference" not in hints


def test_plain_preference_untouched():
    """没有否定词时行为与修复前完全一致。

    注意用「SUV」而不是「越野车」：`_BODY_HINTS` 只有 轿车/suv/mpv 三个键，
    「越野车」本就不产生 body_type（写测试时先写成「越野车」并断言 suv，
    结果拿到 None——是我的期望错了，不是代码错了）。
    """
    hints = extract_hints("预算20万以内，要SUV")
    assert "avoid" not in hints
    assert hints.get("body_type") == ["suv"]


# ── 邻近约束：否定不得跨半句乱指 ──────────────────────────────────────────
def test_negation_does_not_reach_across_a_long_clause():
    """否定词离得太远就不该管住后面的关键词。

    ⚠️ 第一版这条写的是「我今天不太想买换购的这台老车」——**句子里根本没有车型
    关键词**，于是无论邻近窗口在不在都判不出 avoid，测试恒绿（反向验证实测：
    删掉窗口仍全绿）。现在用同一分句里真有 SUV、且否定词离它超过 12 字的句子。
    """
    far = "我今天真的非常非常不想再折腾换车这件事了不过SUV还是可以考虑一下"
    assert "SUV" in far
    assert _negated_values(far.lower()) == [], "相距过远的否定词不该管住后面的 SUV"
    assert "avoid" not in extract_hints(far)

    # 反面对照：同距离内、紧邻时就该生效（证明上面不是「否定词一律无效」）
    near = "不想开MPV"
    assert _negated_values(near.lower()) == ["mpv"]
