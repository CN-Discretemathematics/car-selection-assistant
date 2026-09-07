"""汽车之家月度销量榜抓取→导入 CLI（逻辑见 app/sources/autohome.py）。

用法：
    python tools/fetch_autohome_sales.py [--month YYYY-MM] [--output payload.json]
                                        [--do-import] [--dry-run]

自动获取请用 tools/fetch_sales_scheduled.py（每日触发、幂等、未发布自动重试）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.catalog.services import latest_full_month  # noqa: E402
from app.sources.autohome import build_payload, fetch_rank_page, parse_rank_page  # noqa: E402
from app.sources.fetcher import record_snapshot  # noqa: E402


def fetch_and_import_month(
    month: str,
    *,
    do_import: bool = True,
    dry_run: bool = False,
    output: str | None = None,
    require_month_match: bool = False,
) -> dict:
    """抓取指定月份榜单并（可选）导入数据库。

    返回报告 dict：
    - requested_month：请求的月份；
    - month：门户页面实际公布的月份（数据未发布时可能仍是上一月）；
    - rows：榜单行数；imported/created/updated：导入结果；
    - payload_path/snapshot_path：本地存档路径；
    - skipped_reason：require_month_match 且门户尚未发布目标月时非空，
      此时不生成载荷、不导入（自动脚本据此「明日再试」）。

    快照与载荷存档、OSS 上传、来源记录与手动 CLI 行为一致。
    """
    html, url = fetch_rank_page(month)
    parsed = parse_rank_page(html)
    report: dict = {
        "requested_month": month,
        "month": parsed["month"],
        "rows": len(parsed["rows"]),
        "imported": False,
        "created": 0,
        "updated": 0,
        "payload_path": None,
        "snapshot_path": None,
        "skipped_reason": None,
    }
    if require_month_match and parsed["month"] != month:
        report["skipped_reason"] = f"门户尚未发布 {month} 榜单（当前公布 {parsed['month']}）"
        return report

    payload = build_payload(parsed, url)
    # 文件名/快照用门户实际公布月份（评审 P2：手动指定尚未发布的月份时，避免
    # 载荷内容月份与文件名不一致——载荷 month 字段来自页面实际值）
    actual_month = parsed["month"] or month

    out_path = output or os.path.join("snapshots", f"autohome-{actual_month}.json")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    report["payload_path"] = out_path

    snap_dir = os.path.join("snapshots", "raw")
    os.makedirs(snap_dir, exist_ok=True)
    snap_path = os.path.join(snap_dir, f"autohome-rank-{actual_month}.html")
    with open(snap_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    report["snapshot_path"] = snap_path

    from app.common.database import create_all, get_session_factory
    from app.common.oss import snapshot_object_key, upload_file

    create_all()
    factory = get_session_factory()
    with factory() as db:
        # 快照上传 OSS（配置时）；失败仅保留本地存档
        object_key = snapshot_object_key(f"autohome-rank-{actual_month}.html")
        oss_ref = upload_file(snap_path, object_key)
        record_snapshot(
            db,
            url=url,
            source_type="industry_data",
            raw_object_path=oss_ref or snap_path,
            source_name="汽车之家",
            page_or_section=f"销量榜 {actual_month}",
        )
        if do_import and not dry_run:
            from app.sources.importer import import_catalog

            import_report = import_catalog(db, payload)
            db.commit()
            report["imported"] = True
            report["created"] = import_report.created
            report["updated"] = import_report.updated
            report["errors"] = list(import_report.errors)
            report["conflicts"] = list(import_report.conflicts)
        else:
            db.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汽车之家月度销量榜抓取→导入")
    parser.add_argument("--month", default=None, help="YYYY-MM；默认最近完整自然月")
    parser.add_argument("--output", default=None, help="导出载荷 JSON 路径（默认 snapshots/autohome-<month>.json）")
    parser.add_argument("--do-import", action="store_true", help="抓取后直接导入数据库")
    parser.add_argument("--dry-run", action="store_true", help="只生成载荷并校验，不导入")
    args = parser.parse_args(argv)

    month = args.month or latest_full_month()
    try:
        report = fetch_and_import_month(
            month, do_import=args.do_import, dry_run=args.dry_run, output=args.output
        )
    except PermissionError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001
        print(f"抓取失败：{type(err).__name__}: {err}", file=sys.stderr)
        return 1

    print(f"榜单月份：{report['month']}，车系 {report['rows']} 个（Top {report['rows']}）")
    if report["skipped_reason"]:
        print(f"跳过导入：{report['skipped_reason']}（自动获取请用 fetch_sales_scheduled.py）")
        return 0
    print(f"载荷已写入：{report['payload_path']}")
    print(f"快照已存档：{report['snapshot_path']}")
    if args.do_import and not args.dry_run:
        print(f"导入完成：新建 {report['created']}，更新 {report['updated']}")
        if report.get("conflicts"):
            print(f"冲突记录 {len(report['conflicts'])} 条（保留高优先级来源）")
        if report.get("errors"):
            print(f"错误 {len(report['errors'])} 条：")
            for err in report["errors"][:10]:
                print(f"  - {err}")
            return 1
    elif not args.do_import:
        print(f"导入方式：python tools/import_data.py {report['payload_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
