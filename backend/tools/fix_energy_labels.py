"""SKU 能源类型标注复算与修复（默认 dry-run，只报告不改库）。

背景（用户实测反馈）：Agent 按「燃油车」筛选时返回过「银河星耀8 225km EM-i」
「星耀7 220km 四驱远航版」这类插混 SKU。SQL 过滤本身是对的
（`vehicle_variants.energy_type IN ('ICE')`），错在**入库时的标注**：
旧 `classify_variant_energy` 只认款型名里的「DM/插混/PHEV/双擎/EV/增程」等标识词，
厂商自有命名（吉利雷神 EM-i、领克 EM-P、丰田双擎E+、日产 e-POWER）不命中，
就落到「排量非新能源 → ICE」，把带发动机的插混标成了燃油车。

根因已在 `app/sources/autohome_sku.py` 修复（补标识词 + 用「纯电续航/电池能量」
电驱证据细分）。本工具用**同一套生产判定函数**复算历史数据，保证：
- 报告结果 = 重新抓取后的入库结果（不会出现两套口径）；
- 只改判定确实变化的行，同时同步 `powertrain` 文案与车系 `energy_types` 并集。

用法（在 backend/ 目录下）：
    python tools/fix_energy_labels.py                        # dry-run：全量复算 + 报告
    python tools/fix_energy_labels.py --series 星耀           # 只看某个车系
    python tools/fix_energy_labels.py --limit 100 --csv out.csv
    python tools/fix_energy_labels.py --apply                # 实际写库（先 dry-run 确认）

建议顺序：先 dry-run 核对 → `--apply` → 复跑 dry-run 应为 0 条 →
再跑 `python tools/eval_agent.py` 回归推荐口径。
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import SpecFact, VehicleSeries, VehicleVariant  # noqa: E402
from app.sources.autohome_sku import (  # noqa: E402
    _powertrain_label,
    classify_variant_energy,
    ev_evidence,
    has_engine_evidence,
    normalize_displacement,
)

# 展示顺序与 enums.NEW_ENERGY_TYPES 口径一致：新能源在前
CANONICAL_ORDER = ("BEV", "PHEV", "EREV", "HEV", "ICE")
ENERGY_LABEL = {"BEV": "纯电", "PHEV": "插混", "EREV": "增程", "HEV": "油混", "ICE": "燃油"}
# 入库时 powertrain 对带发动机款型直接存排量条件（如「1.5T」）；
# 纯电/新能源存文案（「纯电动」「插电混动」）。历史脏数据里还存在「0」这类占位值。
_DISPLACEMENT_RE = re.compile(r"^\d+(\.\d+)?[TL]?$", re.I)
_CHUNK = 400


def _displacement_of(variant: VehicleVariant, facts: list[SpecFact]) -> str:
    """复算用的排量条件：优先款型上留存的排量文案，其次参数页的排量事实。

    统一经 normalize_displacement() 归一：「0 / 0.0」视为无发动机（汽车之家给
    纯电款型的排量占位），否则会把纯电误判成带发动机。
    """
    powertrain = (variant.powertrain or "").strip()
    if _DISPLACEMENT_RE.match(powertrain):
        disp = normalize_displacement(powertrain)
        if disp:
            return disp
    for fact in facts:
        if "排量" in (fact.fact_key or ""):
            disp = normalize_displacement(fact.fact_value)
            if disp:
                return disp
    return ""


def _business_key(series_id: int, model_year_id: int | None, config_version: str | None,
                  powertrain: str | None, drivetrain: str | None, effective_from) -> tuple:
    """唯一业务键口径，与 uq_variant_business_key 一致（注意：键里含 powertrain）。"""
    return (series_id, model_year_id, config_version or "", powertrain or "",
            drivetrain or "", effective_from)


def repair_energy_labels(
    db: Session,
    series_filter: str = "",
    on_sale_only: bool = False,
    apply: bool = False,
) -> dict:
    """按生产判定函数复算 SKU 能源类型；apply=False 时只报告不写库。

    返回 {"checked", "changes", "by_transition", "series_updates",
    "series_union_skipped", "skipped_conflicts"}：
    changes 为逐条变更记录（conflict=True 表示因唯一键冲突暂缓写库），
    series_updates 为需要同步的车系能源并集，series_union_skipped 表示因
    on_sale_only 过滤而**故意**跳过并集同步（评审 M3），
    skipped_conflicts 为唯一键冲突行（业务键含 powertrain，改写会与既有行撞键，
    须人工去重后复跑；实测：AION V「埃安霸王龙 插电混动」库内有一对同键重复行）。
    """
    stmt = (
        select(VehicleVariant, VehicleSeries)
        .join(VehicleSeries, VehicleVariant.series_id == VehicleSeries.id)
        .order_by(VehicleVariant.series_id, VehicleVariant.id)
    )
    if series_filter:
        stmt = stmt.where(VehicleSeries.name.like(f"%{series_filter}%"))
    if on_sale_only:
        stmt = stmt.where(VehicleVariant.status == "on_sale")
    pairs = db.execute(stmt).all()

    # 唯一键安全阀：先取全量业务键 → 改标注前探测「本次写库后的键」是否已被他人占用。
    # 占用即冲突：dry-run 同样预警、apply 跳过该行（保持原标注），绝不硬写撞键。
    claimed: dict[tuple, int] = {}
    for row in db.execute(
        select(VehicleVariant.id, VehicleVariant.series_id, VehicleVariant.model_year_id,
               VehicleVariant.config_version, VehicleVariant.powertrain,
               VehicleVariant.drivetrain, VehicleVariant.effective_from)
    ).all():
        claimed[_business_key(row[1], row[2], row[3], row[4], row[5], row[6])] = row[0]

    changes: list[dict] = []
    skipped_conflicts: list[dict] = []
    series_union: dict[int, list[str]] = {}
    series_name: dict[int, str] = {}
    checked = 0

    for start in range(0, len(pairs), _CHUNK):
        chunk = pairs[start:start + _CHUNK]
        ids = [v.id for v, _ in chunk]
        facts_by_variant: dict[int, list[SpecFact]] = {}
        for fact in db.scalars(select(SpecFact).where(SpecFact.variant_id.in_(ids))).all():
            facts_by_variant.setdefault(fact.variant_id, []).append(fact)

        for variant, series in chunk:
            checked += 1
            facts = facts_by_variant.get(variant.id, [])
            displacement = _displacement_of(variant, facts)
            has_engine = has_engine_evidence(facts, displacement)
            ev_range_km, battery_kwh = ev_evidence(facts)
            new_type = classify_variant_energy(
                variant.display_name or "",
                displacement,
                has_engine,
                list(series.energy_types or []),
                ev_range_km=ev_range_km,
                battery_kwh=battery_kwh,
            )
            # 并集按「本次修复后的实际标注」计：dry-run 预览 == apply 结果
            effective = variant.energy_type
            if new_type != variant.energy_type:
                new_pt = _powertrain_label(new_type, displacement)
                new_key = _business_key(
                    series.id, variant.model_year_id, variant.config_version,
                    new_pt, variant.drivetrain, variant.effective_from,
                )
                holder = claimed.get(new_key)
                conflict = holder is not None and holder != variant.id
                record = {
                    "variant_id": variant.id,
                    "series_id": series.id,
                    "series_name": series.name,
                    "display_name": variant.display_name,
                    "status": variant.status,
                    "old": variant.energy_type,
                    "new": new_type,
                    "conflict": conflict,
                    "evidence": (
                        f"纯电续航={ev_range_km:g}km 电池={battery_kwh:g}kWh "
                        f"排量={displacement or '新能源'} 发动机事实={'有' if has_engine else '无'}"
                    ),
                }
                changes.append(record)
                if conflict:
                    # 保持原标注：dry-run 预览与 apply 结果一致，冲突行留待人工去重
                    skipped_conflicts.append(record)
                else:
                    claimed[new_key] = variant.id
                    effective = new_type
                    if apply:
                        variant.energy_type = new_type
                        variant.powertrain = new_pt

            series_name[series.id] = series.name
            present = series_union.setdefault(series.id, [])
            if effective not in present:
                present.append(effective)

    # 评审 M3：--on-sale-only 只扫到在售款型，据此重算并集会把车系能源类型收窄成
    # 「仅在售款型的并集」（停售 ICE + 在售 BEV 的车系会被改写成 ["BEV"]），偏离导入
    # 口径（build_sku_payload 的并集覆盖全部款型）。过滤跑时只改款型标注、不动车系并集，
    # 并集留待全量跑同步。
    series_updates: list[dict] = []
    if not on_sale_only:
        for series_id, types in series_union.items():
            if not types:
                continue
            ordered = [t for t in CANONICAL_ORDER if t in types]
            ordered += [t for t in types if t not in CANONICAL_ORDER]  # 未知取值保底不丢
            series = db.get(VehicleSeries, series_id)
            if series is None or list(series.energy_types or []) == ordered:
                continue
            series_updates.append(
                {
                    "series_id": series_id,
                    "series_name": series_name.get(series_id, ""),
                    "old": list(series.energy_types or []),
                    "new": ordered,
                }
            )
            if apply:
                series.energy_types = ordered

    if apply and (changes or series_updates):
        db.commit()

    return {
        "checked": checked,
        "changes": changes,
        "by_transition": Counter(f"{c['old']}→{c['new']}" for c in changes),
        "series_updates": series_updates,
        "series_union_skipped": on_sale_only and bool(series_union),
        "skipped_conflicts": skipped_conflicts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SKU 能源类型标注复算（默认 dry-run）")
    parser.add_argument("--series", default="", help="只复算车系名包含该片段的 SKU")
    parser.add_argument("--on-sale-only", action="store_true", help="只复算在售 SKU")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只报告）")
    parser.add_argument("--limit", type=int, default=40, help="打印的变更样本条数")
    parser.add_argument("--csv", default="", help="导出完整变更清单到 CSV")
    args = parser.parse_args(argv)

    session_factory = get_session_factory()
    with session_factory() as db:
        report = repair_energy_labels(
            db,
            series_filter=args.series,
            on_sale_only=args.on_sale_only,
            apply=args.apply,
        )

    mode = "已写库（--apply）" if args.apply else "dry-run（未改动数据库）"
    print(f"[{mode}] 复算 SKU：{report['checked']} 个")
    conflict_n = len(report["skipped_conflicts"])
    print(f"需要更正：{len(report['changes'])} 个 SKU；分布：{dict(report['by_transition'])}")
    if conflict_n:
        print(
            f"注意：其中 {conflict_n} 条因唯一键冲突暂缓"
            f"（业务键含 powertrain，写库会与既有行撞键），{'已跳过' if args.apply else 'apply 时将跳过'}："
        )
        for c in report["skipped_conflicts"][: args.limit]:
            old = ENERGY_LABEL.get(c["old"], c["old"])
            new = ENERGY_LABEL.get(c["new"], c["new"])
            print(
                f"  {c['series_name']} {c['display_name']} [{c['status']}]"
                f"：{c['old']}({old}) → {c['new']}({new})｜{c['evidence']}"
            )
        print("处理：此类为同业务键重复款型（靠 powertrain 不同区分），请人工核对去重后复跑本工具。")
    print(f"需要同步车系能源并集：{len(report['series_updates'])} 个车系")
    if report.get("series_union_skipped"):
        print(
            "提示：--on-sale-only 只覆盖在售款型，本次已跳过车系能源并集同步"
            "（否则会把并集收窄成「仅在售款型的并集」，见评审 M3）；"
            "需要同步并集请去掉该参数全量复跑。"
        )
    if not report["changes"] and not report["series_updates"]:
        print("标注与生产判定一致，无需修复。")
        return 0

    print("-" * 72)
    for change in report["changes"][: args.limit]:
        old = ENERGY_LABEL.get(change["old"], change["old"])
        new = ENERGY_LABEL.get(change["new"], change["new"])
        print(
            f"{change['series_name']} {change['display_name']} [{change['status']}]"
            f"：{change['old']}({old}) → {change['new']}({new})｜{change['evidence']}"
        )
    if len(report["changes"]) > args.limit:
        print(f"…另有 {len(report['changes']) - args.limit} 条未打印（--limit 调大或 --csv 导出）")
    for update in report["series_updates"][: args.limit]:
        print(
            f"车系并集 {update['series_name']}：{update['old']} → {update['new']}"
        )

    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["variant_id", "series_id", "series_name", "display_name",
                            "status", "old", "new", "conflict", "evidence"],
            )
            writer.writeheader()
            writer.writerows(report["changes"])
        print(f"完整清单已导出：{args.csv}")

    if not args.apply:
        print("-" * 72)
        print("确认无误后执行：python tools/fix_energy_labels.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
