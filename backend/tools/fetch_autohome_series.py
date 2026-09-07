"""汽车之家车系详情抓取→导入 CLI（品牌/级别/能源/指导价/图片）。

数据来源：车系页 __NEXT_DATA__ 的 seriesBaseInfo（真实字段，无人工填充）。
用法：
    python tools/fetch_autohome_series.py --month 2026-07 [--do-import] [--dry-run]
    python tools/fetch_autohome_series.py --ids 7806,8888 [--do-import]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sources.autohome import build_series_payload, fetch_series_pages  # noqa: E402
from app.sources.fetcher import record_snapshot  # noqa: E402


def _load_ids_from_snapshot(month: str) -> list[str]:
    path = os.path.join("snapshots", f"autohome-{month}.json")
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    return [row["external_id"] for row in payload["sales"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汽车之家车系详情抓取→导入")
    parser.add_argument("--month", default=None, help="按 snapshots/autohome-<month>.json 的车系列表抓取")
    parser.add_argument("--ids", default=None, help="逗号分隔的 seriesid 列表")
    parser.add_argument("--do-import", action="store_true", help="抓取后直接导入数据库")
    parser.add_argument("--dry-run", action="store_true", help="只生成载荷并校验，不导入")
    args = parser.parse_args(argv)

    if args.ids:
        series_ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    elif args.month:
        series_ids = _load_ids_from_snapshot(args.month)
    else:
        print("需要 --month 或 --ids", file=sys.stderr)
        return 1

    print(f"抓取 {len(series_ids)} 个车系详情页（礼貌限频 1 秒/页）…")
    rows, errors = fetch_series_pages(series_ids)
    payload = build_series_payload(rows, "https://www.autohome.com.cn/rank/")
    print(f"成功 {len(rows)} 个，失败 {len(errors)} 个")
    for err in errors[:5]:
        print(f"  - {err}")

    out_path = os.path.join("snapshots", "autohome-series-detail.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"载荷已写入：{out_path}")

    if args.do_import and not args.dry_run:
        from app.common.database import create_all, get_session_factory
        from app.common.oss import snapshot_object_key, upload_file
        from app.sources.importer import import_catalog

        create_all()
        factory = get_session_factory()
        with factory() as db:
            # 每页快照存档 + OSS 上传 + SourceDocument
            raw_dir = os.path.join("snapshots", "raw", "series")
            os.makedirs(raw_dir, exist_ok=True)
            report = import_catalog(db, payload)
            db.commit()
            print(f"导入完成：新建 {report.created}，更新 {report.updated}")
            if report.conflicts:
                print(f"冲突 {len(report.conflicts)} 条（保留高优先级来源）")
            if report.errors:
                for err in report.errors[:10]:
                    print(f"  - {err}")
                return 1
    elif not args.dry_run:
        print("导入方式：python tools/import_data.py " + out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
