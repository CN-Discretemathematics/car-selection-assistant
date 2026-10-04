"""N6-A：参数探针的截断**必须披露**（2026-10-05 生产实测）。

实拍原样（星愿，问「续航和电池容量分别是多少」）：

    核心参数：… 续航 480 km（CLTC）；电池 47.14 kWh
    你问到的相关参数：CLTC纯电续航里程(km) = 310 km（CLTC） / 410 km（CLTC）（不同款型存在差异）

库内实有 3 个续航档（310/310/410/410/480/480）。`entries[:2]` 恰好把**头条那个 480**
切掉，而旧文案只说「不同款型存在差异」——用户以为看到的就是全部，
**拿到的答案里没有那个与上文自相矛盾的值**。

这与 P1-3（佐证被 `text[:60]` 腰斩成「核心参数与配置：级别 = 紧…」）是同一类缺陷：
**截断了但没说截断**。
"""
from __future__ import annotations

from app.agent.series_qa import _PROBE_MAX_KEYS, _PROBE_VALUE_MAX, probe_facts

# 星愿式事实：同一键 3 个去重取值，顺序即 DB 返回顺序（310/410/480）
THREE_VALUES = [
    ("纯电续航里程", "310", "km", "CLTC"),
    ("纯电续航里程", "310", "km", "CLTC"),
    ("纯电续航里程", "410", "km", "CLTC"),
    ("纯电续航里程", "410", "km", "CLTC"),
    ("纯电续航里程", "480", "km", "CLTC"),
    ("纯电续航里程", "480", "km", "CLTC"),
]


def test_value_truncation_is_disclosed():
    """3 个取值只列 2 个时，必须说清「共几个、只列了几个」。"""
    out = probe_facts(THREE_VALUES, "续航是多少")
    line = next(ln for ln in out if "纯电续航里程" in ln)
    assert f"共 3 个取值，此处只列前 {_PROBE_VALUE_MAX} 个" in line, f"截断未披露：{line}"


def test_value_truncation_line_still_shows_two_values():
    """披露不等于不展示：前 2 个取值仍要给出。"""
    out = probe_facts(THREE_VALUES, "续航是多少")
    line = next(ln for ln in out if "纯电续航里程" in ln)
    assert "310" in line and "410" in line


def test_two_values_keeps_old_wording():
    """只有 2 个取值时用旧文案，不制造多余的「共 N 个」噪音。"""
    two = [r for r in THREE_VALUES if r[1] in ("310", "410")]
    out = probe_facts(two, "续航是多少")
    line = next(ln for ln in out if "纯电续航里程" in ln)
    assert "不同款型存在差异" in line
    assert "共 2 个取值" not in line


def test_single_value_has_no_suffix():
    """单值不该带任何后缀。"""
    one = [r for r in THREE_VALUES if r[1] == "480"]
    out = probe_facts(one, "续航是多少")
    line = next(ln for ln in out if "纯电续航里程" in ln)
    assert "差异" not in line and "只列前" not in line


def test_key_truncation_is_disclosed():
    """命中的相关键超过 _PROBE_MAX_KEYS 时，也必须说明还有多少没列。"""
    facts = [(f"电池相关键{i}", str(i), None, None) for i in range(12)]
    out = probe_facts(facts, "电池")
    assert any("个相关参数未列出" in ln for ln in out), f"键截断未披露：{out}"


def test_key_truncation_disclosure_counts_correctly():
    """披露的数量必须是**真实被藏起来的条数**。

    ⚠️ 这里曾写成 `expected_hidden = 12 - len(shown)`——从**实际展示行数反推**，
    于是上限一改，两边同步变化、断言恒成立 → **假绿灯**（审查实测：把
    `_PROBE_MAX_KEYS` 8→7，本测试仍全绿）。上限必须写成**字面量**，
    让它成为被钉住的值而不是被反推的结果。
    """
    facts = [(f"电池相关键{i}", str(i), None, None) for i in range(12)]
    out = probe_facts(facts, "电池")
    # 12 个命中键，最多展示 8 个 → 必须藏起 4 个
    assert any("另有 4 个相关参数未列出" in ln for ln in out), out


def test_key_cap_is_pinned_to_eight():
    """把 `_PROBE_MAX_KEYS = 8` 本身钉住——它决定了「藏了几个」这个数字。"""
    assert _PROBE_MAX_KEYS == 8, "改了截断上限必须同步更新披露数量的期望值与本断言"


def test_value_cap_is_pinned_to_two():
    """`_PROBE_VALUE_MAX = 2` 同理：N6 的披露文案直接依赖它。"""
    assert _PROBE_VALUE_MAX == 2
