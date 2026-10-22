# -*- coding: utf-8 -*-
"""家庭人数的另一种说法：「一家四口」「四口之家」（2026-10-22 生产实测补）

## 缺陷（生产实测，不是推演）

2026-10-22 生产实测跑 12 条代表性探针时，「预算35万，一家四口，长途出行SUV」
这一轮**推 0 款**——不是库里没车，是 Agent 认为用户**没说人数**，转去追问
「几口人？」。根因：人数正则要求带「人」字（「四口人」「4 人」✓），
于是**不带「人」**的家庭说法一律解析不出来：

| 说法 | 修复前 | 修复后 |
| --- | --- | --- |
| 「一家四口」 | `None` | 4 |
| 「四口之家」 | `None` | 4 |
| 「一家五口」 | `None` | 5 |
| 「我们一家5口」 | `None` | 5 |
| 「四口人」 | 4 | 4（**不变**） |
| 「5人以上」 | 6 | 6（**不变**，开区间 +1） |
| 「6座」 | 6 | 6（**不变**，座位侧含端点） |

## 本文件的判据

1. 家庭说法能解析出**确切人数**；
2. **同一语义两种写法结果一致**（「一家四口」==「四口人」）——同一件事写两遍必然漂移；
3. **现有表达逐位不变**（开区间 +1、座位含端点）；
4. **明确的座位硬约束优先**（「一家四口，要6座的车」仍是 6 座）；
5. **不误判**：「三两口菜」「两口锅」不是人数。
"""
from app.catalog.series_constraints import parse_passengers


# ── 1. 家庭说法能解析出确切人数 ────────────────────────────────────────────


def test_household_prefix_one_family_n_kou():
    assert parse_passengers("一家四口") == 4
    assert parse_passengers("一家五口") == 5
    assert parse_passengers("一家2口") == 2
    assert parse_passengers("一家十口") == 10


def test_household_suffix_n_kou_family():
    assert parse_passengers("四口之家") == 4
    assert parse_passengers("五口之家") == 5
    assert parse_passengers("3口之家") == 3


def test_household_inside_sentence():
    assert parse_passengers("预算35万，一家四口，长途出行SUV") == 4
    assert parse_passengers("我们一家5口，预算20万") == 5
    assert parse_passengers("四口之家，预算20万") == 4


# ── 2. 同一语义两种写法必须一致 ────────────────────────────────────────────


def test_household_phrase_equals_people_phrase():
    """「一家四口」与「四口人」是**同一件事的两种写法**——结果必须一致。

    这条是「同一件事写多遍必然漂移」的反面断言：修一个说法不能顺手改另一个。
    """
    for cn, digit in (("四", 4), ("五", 5), ("六", 6)):
        assert parse_passengers("一家%s口" % cn) == digit
        assert parse_passengers("%s口人" % cn) == digit
        assert parse_passengers("%s口之家" % cn) == digit


# ── 3. 现有表达逐位不变 ────────────────────────────────────────────────────


def test_people_branch_unchanged():
    """人数分支：开区间 +1、含端点不加——产品拍板的口径一个字都不动。"""
    assert parse_passengers("5人以上") == 6
    assert parse_passengers("5人开以上") == 6
    assert parse_passengers("5人或以上") == 6
    assert parse_passengers("5人以内") == 5
    assert parse_passengers("5人以下") == 5
    assert parse_passengers("5人") == 5
    assert parse_passengers("5口人") == 5
    assert parse_passengers("一家四口人") == 4  # 带「人」时仍走原分支


def test_seats_branch_unchanged():
    """座位侧含端点（不 +1）——「找台 5 座以上的」不该被反向要 6 座。"""
    assert parse_passengers("6座") == 6
    assert parse_passengers("5座以上") == 5
    assert parse_passengers("5座以内") == 5


# ── 4. 明确的座位硬约束优先 ────────────────────────────────────────────────


def test_explicit_seats_win_over_household_fallback():
    """「一家四口，要6座的车」→ 6 座。

    兜底只在**人数与座位两个分支都没命中**时生效；
    明确的座位硬约束不能被兜底抢走（否则会把用户的车砍到 4 座）。
    """
    assert parse_passengers("一家四口，要6座的车") == 6
    assert parse_passengers("四口之家，预算20万，要7座") == 7


# ── 5. 不误判 ──────────────────────────────────────────────────────────────


def test_not_household_size():
    """「三两口菜」「两口锅」不是人数——**N口 不能裸匹配**。

    这条是「N口之家 / 一家N口」必须带家庭语境的原因：
    裸匹配会把「三两口」读成 3 口。
    """
    assert parse_passengers("三两口菜") is None
    assert parse_passengers("来两口锅") is None
    assert parse_passengers("四口") is None


def test_no_number_no_size():
    assert parse_passengers("预算20万，家用SUV") is None
    assert parse_passengers("") is None
    assert parse_passengers("你好") is None
