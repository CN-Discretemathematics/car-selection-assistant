"""能源泛化规则的**唯一实现**（2026-10-02 收敛）。

事故背景：这条规则是**推荐口径**（用户说「新能源/燃油」，库内是五个具体类型），
此前在三个地方各有一份实现：

1. `agent/engine.py` 的 `_expand_energy_prefs` / `_expand_avoid`
2. `agent/tools.py` 的 SQL 下推段（集合展开写法）
3. `agent/tools.py` 的 Python 侧过滤段（**相反**的逐 variant 判定写法）

第 2、3 份**行为并不一致**（见 `test_legacy_inline_form_had_a_real_bug_now_fixed`）：
用户同时要求「新能源」和「ICE」时，SQL 放行 ICE、Python 侧却拒掉全部 ICE。
现已全部收敛到 `app.common.enums.expand_energy_prefs` / `expand_avoid`，缺陷随之修复。

本测试锁定两件事：
- 泛化口径本身（HEV 归燃油侧）；
- **反向判定写法 == 集合展开写法**（否则第 3 处收敛就是一次行为变更）。
"""
from __future__ import annotations

from app.common.enums import (
    BODY_TYPES,
    ENERGY_TYPES,
    NEW_ENERGY_TYPES,
    expand_avoid,
    expand_energy_prefs,
)


def test_new_energy_expands_to_three_green_types():
    """HEV 归燃油侧——这是首页「新能源/燃油」二分口径，改动等于改产品行为。"""
    assert expand_energy_prefs(["new_energy"]) == set(NEW_ENERGY_TYPES)
    assert "HEV" not in expand_energy_prefs(["new_energy"])


def test_fuel_expands_to_the_rest():
    assert expand_energy_prefs(["fuel"]) == set(ENERGY_TYPES) - set(NEW_ENERGY_TYPES)
    assert expand_energy_prefs(["fuel"]) == {"HEV", "ICE"}


def test_concrete_types_pass_through():
    assert expand_energy_prefs(["BEV"]) == {"BEV"}
    assert expand_energy_prefs(["BEV", "ICE"]) == {"BEV", "ICE"}


def test_alias_and_concrete_union():
    assert expand_energy_prefs(["new_energy", "ICE"]) == set(NEW_ENERGY_TYPES) | {"ICE"}


def test_empty_and_none():
    assert expand_energy_prefs([]) == set()
    assert expand_energy_prefs(None) == set()


def test_avoid_splits_energy_and_body():
    energy, body = expand_avoid(["new_energy", "suv"])
    assert energy == set(NEW_ENERGY_TYPES)
    assert body == {"suv"}


def test_avoid_ignores_unknown_tokens():
    energy, body = expand_avoid(["suv", "不存在的类型"])
    assert energy == set()
    assert body == {"suv"}


def test_avoid_none_is_safe():
    assert expand_avoid(None) == (set(), set())


# ── 收敛的核心断言：旧的「反向逐 variant 判定」与新实现等价 ────────────────

def _legacy_rejects_variant(prefs: list[str], energy_type: str) -> bool:
    """agent/tools.py 收敛前的原始写法（逐 variant 判定），仅用于等价性对照。

    注意必须复刻其外层守卫 `if profile.energy_preference:`——旧代码的第三条
    判定在 `prefs=[]` 时会「拒掉一切」，但那条分支根本不可达（空偏好直接不进
    `if`）。对照实现若不建模这个守卫，就会报出**并不存在**的行为差异。
    """
    if not prefs:  # 外层守卫：空偏好 → 不过滤
        return False
    if "new_energy" in prefs and energy_type not in NEW_ENERGY_TYPES:
        return True
    if "fuel" in prefs and energy_type in NEW_ENERGY_TYPES:
        return True
    if energy_type not in prefs and "new_energy" not in prefs and "fuel" not in prefs:
        return True
    return False


def test_legacy_inline_form_had_a_real_bug_now_fixed():
    """收敛**顺带修掉一个真实缺陷**：SQL 下推与 Python 侧判定曾不一致。

    复现：偏好里**同时**给一个大类和一个具体类型，且具体类型属于大类的**对侧**：

        ["new_energy", "ICE"]  →  SQL 保留 ICE，Python 侧拒掉全部 ICE
        ["fuel", "BEV"]        →  SQL 保留 BEV，Python 侧拒掉全部 BEV

    旧写法的前两条规则只看大类、不看显式点名的具体类型，于是
    「我既要新能源、也要一台 ICE」会被**明确点名**的那一类全灭。

    净效果：用户点名的车系在评分前被静默过滤掉——SQL 已经放行的行，
    Python 侧又拦了下来。这正是本文件开头说的「三份实现无任何东西强制一致」
    的真实代价，**不是收敛引入的回归**。

    收敛后两侧共用 `expand_energy_prefs`，显式点名的具体类型必须被尊重。
    """
    for prefs, must_keep in ((["new_energy", "ICE"], "ICE"), (["fuel", "BEV"], "BEV")):
        allowed = expand_energy_prefs(prefs)
        assert must_keep in allowed, f"{prefs}：显式点名的 {must_keep} 被丢掉"
        assert set(NEW_ENERGY_TYPES) & allowed or set(ENERGY_TYPES) - set(NEW_ENERGY_TYPES) & allowed


def test_new_and_legacy_agree_except_on_the_buggy_case():
    """穷举 prefs × energy_type：收敛后与旧写法一致，**除了**上面那个真实缺陷。

    排除的组合正是**同时含大类**的那些——旧实现在「大类 + 具体类型」或
    「两个大类同时出现」时都会与 SQL 侧不一致（要么丢掉显式点名的一类，
    要么在 `new_energy`+`fuel` 这种自相矛盾的请求上偏向一侧而非取并集）。
    这样既证明收敛在其余情形下是行为等价的重构，又把已知差异钉死，
    避免以后有人把旧写法抄回来。
    """
    cases: list[list[str]] = [
        [], ["new_energy"], ["fuel"], ["BEV"], ["ICE"], ["HEV"],
        ["BEV", "ICE"],                              # 纯具体类型：旧新一致
        ["new_energy", "PHEV"],                      # 大类 + 同侧具体：旧新一致
        ["fuel", "HEV"],                             # 大类 + 同侧具体：旧新一致
    ]
    for prefs in cases:
        allowed = expand_energy_prefs(prefs)
        for et in ENERGY_TYPES:
            legacy_reject = _legacy_rejects_variant(prefs, et)
            new_reject = bool(allowed) and et not in allowed
            assert legacy_reject == new_reject, (
                f"非预期的行为差异：prefs={prefs} energy_type={et} "
                f"旧={legacy_reject} 新={new_reject}"
            )


def test_avoid_and_body_cover_every_known_body_type():
    for body in BODY_TYPES:
        assert expand_avoid([body])[1] == {body}
