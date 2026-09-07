"""数据导入 CLI（数据管线入口）。

用法：
    python tools/import_data.py path/to/payload.json [--dry-run]

payload 结构见 app/sources/importer.py 的 validate_payload 与 tests/test_importer.py 示例；
生产数据需先经过授权确认（乘联会授权为阶段 3 门禁），本工具只负责校验与入库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common.database import create_all, get_session_factory  # noqa: E402
from app.sources.importer import import_catalog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入车型/价格/配置/销量数据")
    parser.add_argument("path", help="JSON 数据文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只做校验，不写库")
    args = parser.parse_args(argv)

    try:
        with open(args.path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        print(f"错误：文件不存在：{args.path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as err:
        print(f"错误：JSON 解析失败：{err}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("错误：JSON 根节点必须是对象。", file=sys.stderr)
        return 1

    if args.dry_run:
        from app.sources.importer import validate_payload

        errors = validate_payload(payload)
        if errors:
            print("校验失败：")
            for err in errors:
                print(f"  - {err}")
            return 1
        print("校验通过（dry-run 未写库）。")
        return 0

    create_all()
    factory = get_session_factory()
    with factory() as db:
        report = import_catalog(db, payload)

    print("导入完成：")
    print(f"  新建：{report.created}")
    print(f"  更新：{report.updated}")
    if report.conflicts:
        print(f"  冲突（保留高优先级来源，已记录 data_quality_conflicts）：{len(report.conflicts)} 条")
        for c in report.conflicts[:10]:
            print(f"    - {c}")
    if report.errors:
        print(f"  错误（已回滚，未写库）：{len(report.errors)} 条")
        for err in report.errors[:20]:
            print(f"    - {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
