# -*- coding: utf-8 -*-
"""把「去重后仍同键冲突」的存量事实按源接口 itemtype 改名（零信息损失，只改键名）。

背景：源接口对混动车给出两行同名「最大功率(kW)」（itemtype=电动机/系统综合 与 发动机），
解析层已保留 itemtype 并对**新抓取**的数据加前缀（发动机-最大功率(kW)）；本脚本把**存量**
的这类冲突行同样改名，使规范键（最大功率(kW)）确定性地指向系统综合口径。

匹配方式（无需存储 specid）：
  1. 找出仍同键冲突的 (variant_id, fact_key)（多行且值不同）；
  2. 按车系取源参数配置（external_series_refs → fetch_sku_config）；
  3. DB display_name == 源 specname → 得到该款型的 specid；
  4. 该参数的各 occurrence（按源顺序）在 specid 上的取值，与 DB 行的值一一对应
     （冲突行值互不相同 → 匹配唯一）；
  5. occurrence[0] 保留规范键，occurrence[k>0] 改名为 f"{itemtype_k}-{key}"。

用法（默认 dry-run）：
    python tools/rename_conflicting_facts.py                 # 只报告
    python tools/rename_conflicting_facts.py --apply --backup /tmp/rename_backup.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select, update  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import ExternalSeriesRef, SpecFact, VehicleVariant  # noqa: E402
from app.sources.autohome_sku import fetch_sku_config  # noqa: E402


def _conflicts(session) -> dict[int, dict[str, list[SpecFact]]]:
    """返回 {variant_id: {fact_key: [按 id 排序的行]}}，仅保留值不同的同键多行。"""
    pairs = session.execute(
        select(SpecFact.variant_id, SpecFact.fact_key)
        .group_by(SpecFact.variant_id, SpecFact.fact_key)
        .having(func.count() > 1)
    ).all()
    conflicts: dict[int, dict[str, list[SpecFact]]] = defaultdict(dict)
    for variant_id, fact_key in pairs:
        rows = session.scalars(
            select(SpecFact).where(SpecFact.variant_id == variant_id, SpecFact.fact_key == fact_key)
            .order_by(SpecFact.id)
        ).all()
        distinct = {str(r.fact_value or "").strip() for r in rows}
        if len(distinct) > 1:
            conflicts[variant_id][fact_key] = rows
    return conflicts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按源 itemtype 改名存量冲突事实（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="执行改名（默认只报告）")
    ap.add_argument("--backup", default=None, help="改名前把变更导出到该 JSON 路径")
    ap.add_argument("--limit-series", type=int, default=None, help="只处理前 N 个车系（灰度）")
    ap.add_argument("--show", type=int, default=10, help="打印跳过明细的条数（默认 10）")
    args = ap.parse_args(argv)

    factory = get_session_factory()
    with factory() as session:
        conflicts = _conflicts(session)
        if not conflicts:
            print("无剩余同键冲突 ✓")
            return 0

        variant_series = {
            row.id: row.series_id
            for row in session.scalars(select(VehicleVariant).where(VehicleVariant.id.in_(list(conflicts))))
        }
        series_ids = sorted(set(variant_series.values()))
        if args.limit_series:
            series_ids = series_ids[:args.limit_series]
        print(f"冲突款型 {len(conflicts)} 个，涉及车系 {len(series_ids)} 个"
              f"（本次处理 {len(series_ids) if not args.limit_series else min(len(series_ids), args.limit_series)} 个）")

        renames: list[dict] = []
        skipped: list[str] = []
        for sid in series_ids:
            ext_rows = session.scalars(
                select(ExternalSeriesRef.external_id).where(ExternalSeriesRef.series_id == sid)
            ).all()
            if not ext_rows:
                skipped.append(f"series {sid}: 无外部映射，跳过")
                continue
            ext_id = str(ext_rows[0])
            try:
                parsed = fetch_sku_config(ext_id)
            except Exception as err:  # noqa: BLE001 — 单车系失败不阻塞整体，报告后继续
                skipped.append(f"series {sid}: 源配置取用失败 {type(err).__name__}")
                continue

            specid_by_name = {v["specname"]: v["specid"] for v in parsed["variants"]}
            key_occurrences: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
            for group in parsed["groups"]:
                for item in group["items"]:
                    key_occurrences[item["key"]].append(
                        (item.get("itemtype") or "", {str(k): str(v) for k, v in item["values"].items()})
                    )

            variants = session.scalars(
                select(VehicleVariant).where(VehicleVariant.id.in_(
                    [vid for vid, keys in conflicts.items() if variant_series.get(vid) == sid]
                ))
            ).all()
            for variant in variants:
                specid = specid_by_name.get(variant.display_name)
                if not specid:
                    skipped.append(f"variant {variant.id}：源页面无同名款型（{variant.display_name}）")
                    continue
                for key, rows in conflicts.get(variant.id, {}).items():
                    occurrences = key_occurrences.get(key) or []
                    if len(occurrences) < 2:
                        continue
                    used: set[int] = set()
                    for row in rows:
                        value = str(row.fact_value or "").strip()
                        matched = None
                        for idx, (itemtype, values) in enumerate(occurrences):
                            if idx in used:
                                continue
                            if values.get(specid) == value:
                                matched = (idx, itemtype)
                                used.add(idx)
                                break
                        if matched is None:
                            skipped.append(f"variant {variant.id} {key}={value!r}：源 occurrence 无同值，跳过")
                            continue
                        idx, itemtype = matched
                        if idx == 0:
                            continue  # 首个 occurrence 保留规范键
                        new_key = f"{itemtype}-{key}" if itemtype else f"{key}（{idx + 1}）"
                        renames.append({
                            "id": row.id, "variant_id": variant.id, "series_id": sid,
                            "old_key": key, "new_key": new_key, "value": value,
                        })

        print(f"\n计划改名 {len(renames)} 行；跳过 {len(skipped)} 项：")
        for s in skipped[:args.show]:
            print(f"  {s}")
        by_new: Counter = Counter(r["new_key"] for r in renames)
        print("改名后键分布（前 10）：")
        for new_key, count in by_new.most_common(10):
            print(f"  {new_key}: {count}")
        if not renames:
            print("无需改名 ✓")
            return 0
        if not args.apply:
            print("\n[dry-run] 未写库。确认无误后加 --apply 执行。")
            return 0
        if args.backup:
            with open(args.backup, "w", encoding="utf-8") as handle:
                json.dump(renames, handle, ensure_ascii=False, indent=1)
            print(f"已留档 {len(renames)} 条 → {args.backup}")
        for r in renames:
            session.execute(
                update(SpecFact).where(SpecFact.id == r["id"]).values(fact_key=r["new_key"])
            )
        session.commit()
        print(f"\n已改名 {len(renames)} 行 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
