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
    """单档 → 单值逐字不变。实测 388/784 个车系属这一类，零影响。"""
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


# ── 安全阀：单位 / 测试口径不一致 → 退回单值 ────────────────────────────────
def test_mixed_units_fall_back_to_single_value():
    """kW 与 Ps 混排时**不得**合成区间（1156 Ps 与 850 kW 不可比）。

    旧代码本来就只取首个单位组比极值；这里钉住的是：区间逻辑不能绕过这道阀。
    实测 513 个车系命中此情形。
    """
    out = rank_headlines(
        {1: [
            (F_POWER, "200", None, None),
            (F_HP, "1156", None, None),
        ]}
    )
    assert "~" not in out[1]["动力"], out
    assert out[1]["动力"] in ("200 kW", "1156 Ps"), out


def test_mixed_cycles_fall_back_to_single_value():
    """综合续航与纯电续航（**不同 fact_key**）**不得**合成一个区间。

    「汉」正是这个形状：DM-i 报 `CLTC纯电续航里程` 125/245km，EV 报
    `CLTC综合续航` 605/635/705km。两者 unit 都是 km、cycle 都是 CLTC，
    只查 unit+cycle 会合成出 `125~705 km`——让用户以为纯电续航能到 705。
    首个实现就漏了 fact_key 这道阀，被本用例抓出来。
    """
    out = rank_headlines(
        {1: [
            (F_RANGE, "605", None, "CLTC"),
            ("CLTC纯电续航里程(km)", "125", None, "CLTC"),
        ]}
    )
    assert "~" not in out[1]["续航"], out
    assert out[1]["续航"] == "605 km（CLTC）", out


def test_range_uses_the_cohort_of_the_extreme_not_all_keys():
    """区间只取**极值所在那一组**的取值，不把别的 fact_key 拉进来。

    汉的真实形状：综合续航 {605, 635, 705}（EV），纯电续航 {125, 245}（DM-i）。
    max 是 705（来自综合续航），所以区间是 `605~705`——DM-i 的 125 不参与，
    既不出现 `125~705` 这种量纲错误的数，也不退回单值丢掉区间信息。
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
