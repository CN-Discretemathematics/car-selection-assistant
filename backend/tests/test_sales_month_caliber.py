"""「最新销量月份」的口径只有**一处**实现（2026-10-21，修 P4 台账 P6.2）。

## 漂移是怎么发生的

同一件事此前有 **3 份** `max(MonthlySales.month)` 查询，散在三个文件里，
而它们的**口径并不相同**：

| 位置 | 原来做什么 | 口径 |
| --- | --- | --- |
| `app/rag/ingest.py` | 入库要抓哪个月的数据 | 零售/门户，**不回退** |
| `app/rag/service.py` | `/ops/rag` 的水位对比 | **任意口径**，不回退 |
| `tools/build_retrieval_index.py` | 写构建 marker | **任意口径**，不回退 |

三份实现、三种口径 —— 同一件事写三遍 = 迟早漂。

## 现在

口径全部收口到 `app/catalog/services.py`：
`latest_sales_month`（用户侧：零售/门户 + 无数据时回退最近完整自然月）与
`latest_sales_month_strict`（不回退，口径可选）。

**语义逐条保持不变**——本次只改「谁来实现」，不改「算什么」。

## 守卫

1. 语义：三种口径在数据不同的时候**结果确实不同**（否则「统一口径」就是空话）；
2. 漂移：除 `catalog/services.py` 外，**任何模块都不得再自己写**
   `max(MonthlySales.month)`。
"""
from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy.orm import Session

from app.catalog import services as catalog
from tests.seed import make_brand, make_sales, make_series, make_source

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _seed_sales(db: Session) -> None:
    """造出「三种口径结果不同」的数据。

    - 2026-06 零售（用户看得见）
    - 2026-07 批发（用户看不见，但库里更新）
    """
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)
    series = make_series(db, brand, name="甲车系", energy_types=("BEV",), source=source)
    make_sales(db, series, "2026-06", 100, source=source, sales_type="retail")
    make_sales(db, series, "2026-07", 80, source=source, sales_type="wholesale")
    db.commit()


def test_user_facing_caliber_ignores_wholesale(db_session: Session):
    """用户侧口径 = 零售/门户：批发月不算「有销量数据的月份」。"""
    _seed_sales(db_session)
    assert catalog.latest_sales_month(db_session) == "2026-06"


def test_strict_caliber_matches_the_user_facing_one_when_data_exists(db_session: Session):
    """有数据时，严格口径与用户侧口径**同月**（差别的只是「无数据时回不回退」）。"""
    _seed_sales(db_session)
    assert catalog.latest_sales_month_strict(db_session) == "2026-06"


def test_any_type_caliber_sees_the_newest_month(db_session: Session):
    """水位口径 = 任意口径：库里最新的那个月（2026-07 批发）也要被看到。

    这正是它与用户侧口径**刻意不同**的地方：水位回答「库里有没有更新的销量」，
    用户侧回答「用户看到的月销量是哪个月」。
    """
    _seed_sales(db_session)
    assert catalog.latest_sales_month_strict(db_session, sales_types=None) == "2026-07"


def test_strict_caliber_does_not_fall_back(db_session: Session):
    """严格口径**不回退**最近完整自然月——抓取侧要的是「真有数据的月」。"""
    source = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="测试品牌", source=source)
    make_series(db_session, brand, name="甲车系", energy_types=("BEV",), source=source)
    db_session.commit()
    assert catalog.latest_sales_month_strict(db_session) is None
    # 用户侧口径则回退到最近完整自然月（月初首页不能整体空）
    assert catalog.latest_sales_month(db_session) == catalog.latest_full_month()


def _inlines_latest_sales_month(source: str) -> bool:
    """源码里是否**真的**写了 `max(MonthlySales.month)`。

    用 **AST** 而不是正则：注释与 docstring 里提到这个写法是**正常的**
    （本文件的说明文字就提到它），正则会把说明文字当成实现 → 误报。

    匹配放宽到「任何 `max(<X>.month)`」而不是只认 `MonthlySales` 字面量：
    否则 `max(_ms.month)`（起了个别名）就能绕过守卫——变异测试 M3 实测踩到。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "max"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and arg.attr == "month":
                return True
    return False


def test_no_module_inlines_its_own_latest_sales_month_query():
    """漂移守卫：除 `catalog/services.py` 外，不得再出现 `max(MonthlySales.month)`。

    口径只有一处实现——否则「统一口径」只是把三份漂移挪了个地方。
    """
    offenders: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        if rel.startswith("tests/") or rel == "app/catalog/services.py":
            continue
        if _inlines_latest_sales_month(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, f"这些模块自己写了「最新销量月份」查询（口径会漂）：{offenders}"
