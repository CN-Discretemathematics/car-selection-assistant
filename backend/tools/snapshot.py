"""数据快照导出 CLI。

用法：
    python tools/snapshot.py --output snapshots/snapshot-2026-08-27.json
回滚方式：python tools/import_data.py <快照文件>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common.database import get_session_factory  # noqa: E402
from app.sources.snapshot import export_payload  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出车型目录数据快照（与 import_data 兼容）")
    parser.add_argument(
        "--output",
        default=None,
        help="输出 JSON 路径（默认 snapshots/snapshot-<时间戳>.json）",
    )
    args = parser.parse_args(argv)

    factory = get_session_factory()
    with factory() as db:
        payload = export_payload(db)

    if args.output:
        path = args.output
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join("snapshots", f"snapshot-{stamp}.json")
    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(
        f"快照已导出：{path}（品牌 {len(payload['brands'])}、车系 {len(payload['series'])}、"
        f"销量 {len(payload['sales'])} 条）"
    )
    print("回滚方式：python tools/import_data.py <快照文件>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
