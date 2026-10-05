"""`rank_headlines` 区间口径的测试（N9，2026-10-05 用户拍板「区间口径」）。

**背景**：实测 784 个多款型车系里 396 个（50.5%）的核心参数整句没有任何一款型能
复现——`汉` 会说出「续航 705km（←3 款 EV）；油耗 0.67L（←5 款 DM-i 插混）」，
库里根本没有这台车。改成区间后展示的每个数都是真实存在过的值。

**断言分两类，缺一不可**：
  1. 正向：同单位同口径多档 → 合成 `310~480 km（CLTC）`；
  2. **安全阀**：单位或测试口径不一致时**必须退回单值**，绝不硬拼——
     写出 `125~705 km（CLTC综合续航与纯电续航混排）` 比写错更糟。
"""
from __future__ import annotations

from app.catalog.series_index import rank_headlines

F_RANGE = "CLTC综合续航(km)"
F_POWER = "电动机总功率(kW)"
F_HP = "电动机总马力(Ps)"
F_SIZE = "长*宽*高(mm)"
F_ACCEL = "官方0-100km/h加速(s)"
F_BATTERY = "电池能量(kWh)"


# ── 正向：同单位同口径多档 → 区间 ───────────────────────────────────────────
def test_multi_tier_becomes_range():
    """三档续航 → 区间，且端点是**实际出现过的**最小/最大值。"""
    out = rank_headlines(
        {1: [
            (F_RANGE, "310", None, "CLTC"),
            (F_RANGE, "480", None, "CLTC"),
            (F_RANGE, "410", None, "CLTC"),
        ]}
    )
    assert out[1]["续航"] == "310~480 km（CLTC）", out


def test_min_mode_range_is_still_ascending():
    """min 模式（加速/油耗）同样合成区间——区间永远是「小~大」，不跟 mode 反。"""
    out = rank_headlines(
        {1: [
            (F_ACCEL, "7.9", None, None),
            (F_ACCEL, "6.5", None, None),
        ]}
    )
    assert out[1]["加速"] == "6.5~7.9 s", out


def test_single_tier_stays_single_value():
    """单档 → 单值逐字不变。

    实测 784 个多款型车系里 **174 个（22.2%）**输出逐字不变、610 个（77.8%）会变。
    2026-10-05 订正：本文件初版写的是「388/784 零影响」，388 是「未被拼接」的
    车系数，而**未被拼接 ≠ 文本不变**（星愿 6 款未被拼接，但动力 85→58~85）。
    """
    out = rank_headlines({1: [(F_RANGE, "705", None, "CLTC")]})
    assert out[1]["续航"] == "705 km（CLTC）", out


def test_repeated_identical_values_stay_single_value():
    """三款都是 705km → 仍是一个值（去重后只算一档，不该写成 705~705）。"""
    out = rank_headlines(
        {1: [
            (F_RANGE, "705", None, "CLTC"),
            (F_RANGE, "705", None, "CLTC"),
            (F_RANGE, "705", None, "CLTC"),
        ]}
    )
    assert out[1]["续航"] == "705 km（CLTC）", out


def test_unit_embedded_in_value_is_merged():
    """单位写在值里（「150kW」）：区间要合并成一个后缀，不能写成 150kW~200kW。"""
    out = rank_headlines(
        {1: [
            (F_POWER, "150kW", None, None),
            (F_POWER, "200kW", None, None),
        ]}
    )
    assert out[1]["动力"] == "150~200kW", out


# ── 安全阀：fact_key / 单位 / 测试口径任一不一致 → 退回单值 ─────────────────
def test_mixed_keys_fall_back_to_single_value():
    """`CLTC综合续航` 与 `CLTC纯电续航里程`（**不同 fact_key**）不得合成一个区间。

    两者 unit 都是 km、cycle 都是 CLTC，只查 unit+cycle 会合成出 `605~125` 这种
    把「综合」和「纯电」混为一谈的数。首个实现就漏了 fact_key 这层，被本用例抓红。
    """
    out = rank_headlines(
        {1: [
            (F_RANGE, "605", None, "CLTC"),
            ("CLTC纯电续航里程(km)", "125", None, "CLTC"),
        ]}
    )
    assert "~" not in out[1]["续航"], out
    assert out[1]["续航"] == "605 km（CLTC）", out


def test_same_key_different_unit_is_excluded_from_range():
    """**同一 fact_key 但 unit 不同**的值不得进同一个区间。

    注意这一条钉的**不是** `_range_text` 里 cohort 元组的 `e.unit`（那个条件是冗余的：
    `best` 恒取自 `same_unit` 组，组内 unit 已经全等），而是**既有的 `ref_unit` 分组**——
    改动之前它就在，只是从没被测试覆盖过。审查实测：抹掉 cohort 里的 `e.unit` 后
    本用例仍然全绿，说明真正起作用的是上游那道过滤。

    同一 fact_key 配一个数值更大的异单位值（900 mi）：上游过滤失效就会选到 900，
    拼出 `310~900 km` 这种把英里当公里的区间。
    """
    out = rank_headlines(
        {1: [
            (F_RANGE, "310", "km", "CLTC"),
            (F_RANGE, "480", "km", "CLTC"),
            (F_RANGE, "900", "mi", "CLTC"),
        ]}
    )
    assert out[1]["续航"] == "310~480 km（CLTC）", out


def test_mixed_power_units_fall_back_to_single_value():
    """kW 与 Ps 混排时不得跨单位合成区间（1156 Ps 与 850 kW 不可比）。

    实测 513 个车系命中此情形。两档 kW + 一档数值更大的 Ps：若 `ref_unit` 分组失效，
    极值会选到 1156 Ps，区间变成 `200~1156`——所以这条同样钉住上游分组。
    """
    out = rank_headlines(
        {1: [
            (F_POWER, "200", None, None),
            (F_POWER, "850", None, None),
            (F_HP, "1156", None, None),
        ]}
    )
    assert out[1]["动力"] == "200~850 kW", out


def test_range_uses_the_cohort_of_the_extreme_not_all_keys():
    """区间只取**极值所在那一组**的取值，不把别的 fact_key 拉进来。

    合成用例：综合续航 {605, 635, 705}，纯电续航 {125}。max 是 705（来自综合续航），
    于是区间是 `605~705`——125 不参与，既不出现 `125~705` 这种量纲错误的数，
    也不退回单值丢掉区间信息。

    （真实数据里「汉」不长得这样：它的 40 行续航事实全在 `CLTC纯电续航里程` 一个键，
    `CLTC综合续航` 一行都没有，所以实际输出是 `125~705 km（CLTC）`。
    「综合/纯电分属两键」的形状在库里确实存在，但不在汉身上。）
    """
    out = rank_headlines(
        {1: [
            (F_RANGE, "605", None, "CLTC"),
            (F_RANGE, "705", None, "CLTC"),
            (F_RANGE, "635", None, "CLTC"),
            ("CLTC纯电续航里程(km)", "125", None, "CLTC"),
            ("CLTC纯电续航里程(km)", "245", None, "CLTC"),
        ]}
    )
    assert out[1]["续航"] == "605~705 km（CLTC）", out


def test_same_key_different_cycle_falls_back():
    """同一个 fact_key 但 cycle 字段不同（脏数据）也要退回单值。"""
    out = rank_headlines(
        {1: [
            (F_RANGE, "605", None, "CLTC"),
            (F_RANGE, "705", None, "WLTC"),
        ]}
    )
    assert "~" not in out[1]["续航"], out


# ── 尺寸：text 模式不参与区间 ──────────────────────────────────────────────
def test_size_never_becomes_a_range():
    """长*宽*高 是三元组，`4650~5190*1935*1795` 没有意义——仍取首值。"""
    out = rank_headlines(
        {1: [
            (F_SIZE, "4995*1910*1495", None, None),
            (F_SIZE, "5050*1960*1505", None, None),
        ]}
    )
    assert out[1]["尺寸"] == "4995*1910*1495 mm", out
    assert "~" not in out[1]["尺寸"], out


# ── 回归：极值选择逻辑未变 ────────────────────────────────────────────────
def test_extreme_selection_unchanged_when_range_unavailable():
    """区间被安全阀挡掉时，回退值必须仍是**极值**（max 取大 / min 取小）。

    这是本次改动的核心回归点：只换展示形态，不动取值逻辑。
    """
    out = rank_headlines(
        {1: [
            (F_BATTERY, "29.4", None, None),
            (F_BATTERY, "72", None, None),
            (F_BATTERY, "67.4", None, None),
        ]}
    )
    # 同单位同口径 → 能合成区间
    assert out[1]["电池"] == "29.4~72 kWh", out

    mixed = rank_headlines(
        {1: [
            (F_BATTERY, "29.4", None, None),
            (F_BATTERY, "72", None, "快充"),
        ]}
    )
    assert mixed[1]["电池"] == "72 kWh（快充）", mixed  # max 仍取 72，且带口径后缀


# ── 多值串脏数据：端点含分隔符时退回单值 ───────────────────────────────────
def test_slash_separated_multi_value_falls_back():
    """库里存在「一个 fact_value 里塞多个取值」的脏数据，区间必须退回单值。

    实测命中（AION i60 / AION V / 极狐阿尔法S6 / 极狐阿尔法T6 / 枫叶80v L 共 5 处）：
    电池值形如 `29.165~74.96/75.26`。`_numeric` 只取到 74.96 参与比较，端点却整串
    输出，于是拼出 `29.165~74.96/75.26 kWh`——高端口会被读成 `74.96/75.26` 或
    `75.26`，**比旧单值更难读**。宁可退回单值。
    """
    out = rank_headlines(
        {1: [
            (F_BATTERY, "29.165", None, None),
            (F_BATTERY, "74.96/75.26", None, None),
        ]}
    )
    assert "~" not in out[1]["电池"], out
    assert out[1]["电池"] == "74.96/75.26 kWh", out


def test_whitespace_around_raw_values_is_trimmed():
    """端点首尾空白要 strip，否则会拼出 `310 ~480 km` 这种双空格。"""
    out = rank_headlines(
        {1: [
            (F_RANGE, "  310  ", None, "CLTC"),
            (F_RANGE, "480", None, "CLTC"),
        ]}
    )
    assert out[1]["续航"] == "310~480 km（CLTC）", out
