# -*- coding: utf-8 -*-
"""清理 spec_facts 的同键冲突行（2026-09-16 卡罗拉锐放事故的存量修复）。

背景：源参数页对混动车给出两行同名「最大功率(kW)」（系统综合 144 / 发动机净功率 116），
原解析器直接入库 → 同一款型同一 fact_key 存在多个不同取值。对比分析按行序取值，
不同款型覆盖到不同行，凭空得出「144 vs 116、差 24%」（两款实际同为 144kW）。
解析层已修（`autohome_sku.dedupe_duplicate_keys`，零信息损失），本脚本修**存量行**。

用法（容器内 / 本地均可；默认 dry-run，不写库）：
    python tools/dedupe_conflicting_facts.py                     # 全库报告
    python tools/dedupe_conflicting_facts.py --series-id 10      # 只报告某车系
    python tools/dedupe_conflicting_facts.py --series-id 10 --apply
    python tools/dedupe_conflicting_facts.py --apply             # 全库执行（谨慎）

安全：删除条件与解析层同规则——同键同值只留一行；值不同则仅当**每个**值都能在同款型的
更具体键（系统综合功率/发动机最大功率/净功率/电动机总功率）里读到才去重，否则保留全部
（由对比分析标注「存疑、不参与比较」）。
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

# 与 backend/tools 下其他脚本一致：把 backend 根加入 sys.path，容器内可直接 `python tools/xxx.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, select  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import SpecFact, VehicleSeries, VehicleVariant  # noqa: E402

# 去重规则故意内联：本工具要能对**修复前**的部署执行（那时 app.sources.autohome_sku
# 还没有 dedupe_duplicate_keys）。与 app.sources.autohome_sku.dedupe_duplicate_keys 同规则，
# 由 tests/test_dedupe_conflicting_facts.py 的交叉一致性测试保证两处不漂移。
DEDUPE_PREFERRED_KEYS = (
    "系统综合功率(kW)", "发动机最大功率(kW)", "最大净功率(kW)", "电动机总功率(kW)",
)


def dedupe_duplicate_keys(facts: list[dict]) -> list[dict]:
    """同键多行去重（零信息损失）。规则见 app/sources/autohome_sku.py 同名函数。"""
    by_key: dict[str, list[dict]] = {}
    for fact in facts:
        by_key.setdefault(str(fact.get("fact_key") or ""), []).append(fact)
    values_by_key = {
        key: {str(f.get("value") or "").strip() for f in group} for key, group in by_key.items()
    }
    out: list[dict] = []
    for key, group in by_key.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        distinct = {str(f.get("value") or "").strip() for f in group}
        if len(distinct) == 1:
            out.append(group[0])
            continue
        # 同义不同粒度（如 车身结构「5门5座两厢车」⊃「两厢车」）→ 保留信息量最大的那个
        longest = max(distinct, key=len)
        if all(value == longest or value in longest for value in distinct):
            for fact in group:
                if str(fact.get("value") or "").strip() == longest:
                    out.append(fact)
                    break
            continue
        covered = lambda value: any(  # noqa: E731
            value in values_by_key.get(sibling, set())
            for sibling in DEDUPE_PREFERRED_KEYS if sibling != key
        )
        if not all(covered(value) for value in distinct):
            out.extend(group)
            continue
        kept: dict | None = None
        for sibling in DEDUPE_PREFERRED_KEYS:
            if sibling == key or sibling not in values_by_key:
                continue
            for fact in group:
                if str(fact.get("value") or "").strip() in values_by_key[sibling]:
                    kept = fact
                    break
            if kept is not None:
                break
        out.extend([kept] if kept is not None else group)
    return out


def _candidate_variant_ids(session, series_id: int | None) -> list[int]:
    """先用 SQL 定位「同款型同键多行」的款型（避免全表载入内存——60 万行会 OOM，实测 137）。"""
    stmt = (
        select(SpecFact.variant_id)
        .group_by(SpecFact.variant_id, SpecFact.fact_key)
        .having(func.count() > 1)
    )
    if series_id is not None:
        stmt = stmt.join(VehicleVariant, VehicleVariant.id == SpecFact.variant_id).where(
            VehicleVariant.series_id == series_id
        )
    return sorted({row for row in session.scalars(stmt)})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清理 spec_facts 同键冲突行（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="执行删除（默认只报告）")
    ap.add_argument("--series-id", type=int, default=None, help="限定车系 id")
    ap.add_argument("--show", type=int, default=8, help="打印样例条数（默认 8）")
    ap.add_argument("--chunk", type=int, default=100, help="每批处理的款型数（内存有界）")
    ap.add_argument("--backup", default=None, help="删除前把待删行导出到该 JSON 路径（留档）")
    args = ap.parse_args(argv)

    factory = get_session_factory()
    with factory() as session:
        candidates = _candidate_variant_ids(session, args.series_id)
        series_names = {row.id: row.name for row in session.scalars(select(VehicleSeries))}

        drop_ids: list[int] = []
        by_series: Counter = Counter()
        samples: list[str] = []
        scanned_variants = 0
        for start in range(0, len(candidates), args.chunk):
            chunk = candidates[start:start + args.chunk]
            facts = list(session.scalars(
                select(SpecFact).where(SpecFact.variant_id.in_(chunk))
                .order_by(SpecFact.variant_id, SpecFact.id)
            ))
            by_variant: dict[int, list[SpecFact]] = defaultdict(list)
            for fact in facts:
                by_variant[fact.variant_id].append(fact)
            variant_series = {
                row.id: row.series_id
                for row in session.scalars(
                    select(VehicleVariant).where(VehicleVariant.id.in_(list(by_variant)))
                )
            }
            for variant_id, rows in by_variant.items():
                scanned_variants += 1
                payload = [
                    {"id": r.id, "fact_key": r.fact_key, "value": r.fact_value, "unit": r.unit}
                    for r in rows
                ]
                kept_ids = {item["id"] for item in dedupe_duplicate_keys(payload)}
                for row in rows:
                    if row.id in kept_ids:
                        continue
                    drop_ids.append(row.id)
                    sid = variant_series.get(variant_id)
                    by_series[sid] += 1
                    if len(samples) < args.show:
                        samples.append(
                            f"  variant={variant_id} series={sid}"
                            f"（{series_names.get(sid, '?')}） {row.fact_key} = {row.fact_value}"
                        )

        print(f"候选款型（同键多行）{len(candidates)} 个，实际扫描 {scanned_variants} 个")
        print(f"可安全删除的同键冲突行：{len(drop_ids)}")
        if by_series:
            print("按车系分布（前 10）：")
            for sid, count in by_series.most_common(10):
                print(f"  series {sid}（{series_names.get(sid, '?')}）：{count} 行")
        if samples:
            print("样例：")
            print("\n".join(samples))
        if not drop_ids:
            print("无需修复 ✓")
            return 0
        if not args.apply:
            print("\n[dry-run] 未写库。确认无误后加 --apply 执行。")
            return 0
        if args.backup:
            # 删除前留档：待删行导出为 JSON（可从来源页重新抓取，留档只为可追溯/可回滚核对）
            import json
            rows = session.scalars(select(SpecFact).where(SpecFact.id.in_(drop_ids)))
            payload = [
                {
                    "id": r.id, "variant_id": r.variant_id, "fact_key": r.fact_key,
                    "fact_value": r.fact_value, "unit": r.unit, "source_id": r.source_id,
                }
                for r in rows
            ]
            with open(args.backup, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            print(f"已留档 {len(payload)} 行 → {args.backup}")
        session.execute(delete(SpecFact).where(SpecFact.id.in_(drop_ids)))
        session.commit()
        print(f"\n已删除 {len(drop_ids)} 行 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
