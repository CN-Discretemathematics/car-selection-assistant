"""排除语境**显式化**（2026-10-21 结构修复的回归测试）。

## 之前的问题（不是 bug，是**结构**问题）

`agent/preferences.py::resolve_brand_names` 要按「排除」语境解析品牌名，
做法是**拼一句话**再交给解析器：

    hints = resolve_brand_mentions(db, f"不要{clean}")

⇒ 语境只存在于两个**隐式约定**里：
① `brands.NEGATION_PREFIXES` 这个否定词表；② 前 4 字窗口。

那套约定一旦被改（换词表、把窗口从 4 改成 2），这条路径会**静默失效**：
没有报错、没有日志，只是「排除项又不见了」——
而排除项失效**直接毁掉答案**（计划 §1.2：硬约束排除项错了直接毁掉答案）。

2026-10-21 的「歧义品牌排除项静默失效」就是同一类问题的又一次现身。

## 现在的形状

语境是**参数**：`resolve_brand_mentions(db, name, exclude_context=True)`
⇒ 词表怎么改都不影响这条路径。

## 本文件钉住的三条

1. 显式参数**确实**按排除语境解析（命中歧义品牌「大众」）；
2. **解耦**：把否定词表清空，记忆回填**照样**能解析出排除项
   （这条就是「结构真的修好了」的证据——旧的拼字符串做法在这里会红）；
3. **非排除语境的老行为逐字不变**（歧义守卫仍然拦着「大众的预算 20 万」）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.agent.preferences import resolve_brand_names
from app.catalog import brands as brands_module
from app.catalog.brands import resolve_brand_mentions
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed_ambiguous_brand(db_session: Session):
    """造一个**歧义品牌**（日常词品牌：大众）——排除语境下最容易失效的那种。"""
    source = make_source(db_session)
    vw = make_brand(db_session, name="大众", brand_type="domestic", source=source)
    series = make_series(db_session, vw, name="大众迈腾", energy_types=("ICE",), source=source)
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="ICE",
        price_cny="200000", source=source,
    )
    db_session.commit()
    return vw


def test_exclude_context_parses_ambiguous_brand(db_session: Session):
    """显式参数 ⇒ 歧义品牌按排除语境解析（歧义守卫在排除语境下不适用）。"""
    vw = _seed_ambiguous_brand(db_session)

    out = resolve_brand_mentions(db_session, "大众", exclude_context=True)

    assert out.get("brand_exclude_ids") == [vw.id], out
    assert out.get("brand_exclude_labels") == ["大众"], out
    assert "brand_ids" not in out, "排除语境不该同时进正向集合"


def test_memory_refill_does_not_depend_on_negation_word_list(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """**解耦**：否定词表清空后，记忆回填照样能解析出排除项。

    这条是「结构真的修好了」的证据：旧的拼字符串做法（`f"不要{name}"`）
    在这里会红——它把语境寄托在词表上。
    """
    vw = _seed_ambiguous_brand(db_session)

    # 把否定词表整个清空 ⇒ 文本里的否定语境再也识别不出来
    monkeypatch.setattr(brands_module, "NEGATION_PREFIXES", ())

    ids, labels = resolve_brand_names(db_session, ["大众"])

    assert ids == [vw.id], f"排除项回填失效（依赖了否定词表）：ids={ids} labels={labels}"
    assert labels == ["大众"], labels


def test_normal_context_behaviour_is_unchanged(db_session: Session):
    """**非排除语境**的老行为逐字不变：歧义守卫仍然拦着日常词。"""
    _seed_ambiguous_brand(db_session)

    # 歧义品牌在普通语境下仍然需要强意图词
    assert resolve_brand_mentions(db_session, "大众的预算大概是 20 万") == {}
    assert resolve_brand_mentions(db_session, "大众化产品不错") == {}
    # 强意图词仍然认（正向约束）
    assert resolve_brand_mentions(db_session, "我想买大众")["brand_labels"] == ["大众"]
    # 文本里的否定语境仍然照旧生效（不依赖新参数）
    neg = resolve_brand_mentions(db_session, "预算15万，不要大众")
    assert neg.get("brand_exclude_labels") == ["大众"], neg


def test_exclude_context_does_not_leak_into_positive_constraint(db_session: Session):
    """排除语境**不得**同时产出正向约束（否则「不要大众」会变成「只要大众」）。"""
    _seed_ambiguous_brand(db_session)

    out = resolve_brand_mentions(db_session, "大众", exclude_context=True)

    assert "brand_ids" not in out, out
    assert "brand_labels" not in out, out
