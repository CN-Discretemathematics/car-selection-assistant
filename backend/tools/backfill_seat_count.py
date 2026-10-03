"""回填 `vehicle_variants.seat_count`（H2-A 的数据侧，2026-10-03）。

## 口径必须与运行时逐字一致

`app/agent/tools.py::_extract_seats` 的判定是：

    for key in _SEAT_FACT_KEYS:                      # 顺序敏感！
        value = facts.get(("座位数", key), {}).get("value") \
             or facts.get(("参数信息", key), {}).get("value")
        if value is not None:
            return _parse_number(value)              # 取**第一个**能解析的值
    return None                                       # 一个都没有 → None

所以本脚本也必须：
  1. 按 `_SEAT_FACT_KEYS` 的**同一个顺序**逐个键尝试；
  2. 每个键先看 category='座位数'，再退回 category='参数信息'；
  3. 取第一个能解析出数字的值；
  4. 一个都没有 → 留 NULL（**不是**填 0，也不是填 2）。

第 4 条尤其重要：NULL 的含义是「库里没有座位数事实」，运行时必须**保留**这类款型。
把它填成任何数字都会静默改变推荐结果。

## 为什么用 SQL 而不是逐款型遍历

实测语料 6629 款型 / 743276 条事实（每款型约 112 条），逐款型遍历是 74 万次查询。
这里用一条 set-based SQL 按「键优先级」排序后取每款型第一行，O(事实数) 一次扫完。

用法：
    DATABASE_URL=… python tools/backfill_seat_count.py            # 干跑，只报统计
    DATABASE_URL=… python tools/backfill_seat_count.py --apply    # 真写库
"""
from __future__ import annotations

import argparse

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect)
from sqlalchemy import text  # noqa: E402

from app.agent.tools import (  # noqa: E402
    _SEAT_FACT_KEYS,
    _parse_number,
)

# 直接从 tools 侧复用判定表与解析函数——**不复制一份**。复制就会出现
# 「回填用一套口径、运行时用另一套」而无人发现的漂移。
from app.common.database import get_session_factory  # noqa: E402

# 键优先级 → CASE 表达式。顺序必须与 _SEAT_FACT_KEYS 一致（rank 越小越优先）。
_PRIORITY = {key: i for i, key in enumerate(_SEAT_FACT_KEYS)}
# 每个键允许的 category，次序与 _extract_seats 的 `a or b` 一致：先 座位数 后 参数信息。
_CATEGORIES = ("座位数", "参数信息")
# 哨兵：任何一个座位键都没命中的事实都落在这个 prio 上，外层据此判定「该款型无座位数」。
_MAX_PRIO = len(_PRIORITY) * 10 + len(_CATEGORIES)


def _build_sql() -> str:
    """构造「每个 variant 取优先级最高的那条可解析座位数事实」的 SQL。"""
    whens = []
    # 键与 category 的**真实值不拼进 SQL**（SQL 注入面 + 引号转义），只拼参数占位符；
    # 实值在 main() 里通过 params 绑定。因此这里只用到它们的**序号**。
    for rank, _key in enumerate(_SEAT_FACT_KEYS):
        for cat_rank, _cat in enumerate(_CATEGORIES):
            whens.append(
                f"WHEN f.fact_key = :k{rank} AND f.category = :c{cat_rank} "
                f"THEN {rank * 10 + cat_rank}"
            )
    case_sql = "CASE " + " ".join(whens) + f" ELSE {_MAX_PRIO} END"
    # 用 PostgreSQL / SQLite 都支持的写法：subquery + 窗口函数。
    # SQLite 3.25+ 与 PG 9+ 均支持 row_number()，本仓两库都满足。
    #
    # ⚠️ 外层**必须**再挡一层 `prio < _MAX_PRIO`：没有任何座位键命中的款型，它的所有
    # 事实 prio 都等于 _MAX_PRIO，row_number() 会挑出**任意一条**（可能是车型名这种
    # 非数字），而正确行为是**一个都不返回、留 NULL**。我第一版漏了这层，
    # 跑出来直接 `could not convert string to float: '星愿 2026款…'` 才暴露。
    return f"""
        SELECT variant_id, seats FROM (
            SELECT f.variant_id AS variant_id,
                   {case_sql} AS prio,
                   f.fact_value AS seats,
                   ROW_NUMBER() OVER (
                       PARTITION BY f.variant_id ORDER BY {case_sql} ASC
                   ) AS rn
            FROM spec_facts f
            WHERE f.fact_value IS NOT NULL
        ) ranked
        WHERE rn = 1 AND prio < {_MAX_PRIO}
    """


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填 vehicle_variants.seat_count")
    ap.add_argument("--apply", action="store_true", help="真写库（默认只干跑并报统计）")
    args = ap.parse_args(argv)

    sql = _build_sql()
    params: dict[str, object] = {}
    for rank, key in enumerate(_SEAT_FACT_KEYS):
        params[f"k{rank}"] = key
    for cat_rank, cat in enumerate(_CATEGORIES):
        params[f"c{cat_rank}"] = cat

    factory = get_session_factory()
    with factory() as db:
        # 干跑：先在 Python 侧按**运行时口径**独立算一遍，作为交叉校验
        from sqlalchemy import select

        from app.common.models import SpecFact

        runtime: dict[int, float | None] = {}
        rows = db.execute(
            select(SpecFact.variant_id, SpecFact.category, SpecFact.fact_key, SpecFact.fact_value)
        ).all()
        by_variant: dict[int, list[tuple[str, str, str]]] = {}
        for vid, cat, key, val in rows:
            by_variant.setdefault(vid, []).append((cat, key, val))
        for vid, facts in by_variant.items():
            seats = None
            for key in _SEAT_FACT_KEYS:
                val = None
                for cat in _CATEGORIES:
                    for c, k, v in facts:
                        if c == cat and k == key:
                            val = v
                            break
                    if val is not None:
                        break
                if val is not None:
                    n = _parse_number(val)
                    if n is not None:
                        seats = n
                        break
            runtime[vid] = seats

        sql_result = {vid: float(s) for vid, s in db.execute(text(sql), params).all()}
        total = len(runtime)
        got = len(sql_result)
        mismatch = [
            (vid, runtime[vid], sql_result[vid])
            for vid in runtime
            if runtime[vid] is not None
            and (vid not in sql_result or int(sql_result[vid]) != int(runtime[vid]))
        ]
        print(f"款型总数        = {total}")
        print(f"运行时口径能取到 = {sum(1 for v in runtime.values() if v is not None)}")
        print(f"SQL 口径能取到  = {got}")
        print(f"两者不一致      = {len(mismatch)}")
        if mismatch:
            for vid, r, s in mismatch[:5]:
                print(f"  variant {vid}: 运行时={r} SQL={s}")
            print("\n❌ 口径不一致，**拒绝写库**（这正是本脚本要防的事）")
            return 2

        if not args.apply:
            print("\n干跑结束（未写库）。加 --apply 执行。")
            return 0

        upd = text("UPDATE vehicle_variants SET seat_count = :s WHERE id = :vid")
        # 不用 `with db.begin()`：上面的核对查询已经在同一 Session 上开了事务，
        # 再 begin() 会抛 `A transaction is already begun`。直接在既有事务里
        # execute，最后统一 commit 一次即可（回填要么全成要么全不成）。
        for vid, seats in sql_result.items():
            db.execute(upd, {"s": int(seats), "vid": vid})
        db.commit()
        print(f"\n已回填 {len(sql_result)} 个款型的 seat_count。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
