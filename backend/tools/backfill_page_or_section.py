"""回填 SpecFact.page_or_section（评审 M-M6-2）。

历史导入的汽车之家事实行 page_or_section 为 NULL（该字段后补），而当前导入器统一写
「汽车之家参数配置页」。本工具把 NULL 行按来源回填为同一常量，保证检索切片的
page_or_section 元数据完整、可溯源。

用法：python tools/backfill_page_or_section.py [--apply]
    不加 --apply 为 dry-run，只统计不写入。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select, update  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import Source, SpecFact  # noqa: E402

PAGE_LABEL = "汽车之家参数配置页"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填 SpecFact.page_or_section（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认只统计）")
    args = parser.parse_args(argv)

    with get_session_factory()() as db:
        source_ids = list(
            db.scalars(select(Source.id).where(Source.name == "汽车之家"))
        )
        if not source_ids:
            print("未找到来源「汽车之家」，无需处理。")
            return 0
        missing = (
            db.scalar(
                select(func.count())
                .select_from(SpecFact)
                .where(SpecFact.page_or_section.is_(None), SpecFact.source_id.in_(source_ids))
            )
            or 0
        )
        total_facts = (
            db.scalar(
                select(func.count())
                .select_from(SpecFact)
                .where(SpecFact.source_id.in_(source_ids))
            )
            or 0
        )
        print(f"汽车之家事实共 {total_facts} 行，其中 page_or_section 为空 {missing} 行。")
        if missing and args.apply:
            db.execute(
                update(SpecFact)
                .where(SpecFact.page_or_section.is_(None), SpecFact.source_id.in_(source_ids))
                .values(page_or_section=PAGE_LABEL)
            )
            db.commit()
            print(f"已回填 {missing} 行 → 「{PAGE_LABEL}」。")
        elif missing:
            print("dry-run：未写入。确认后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
