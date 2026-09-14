"""车系款型缺口：统计与导出（供回补脚本与巡检使用）。

背景（2026-09-14 用户实测「风云A9 有销量但没款型」）：
销量榜导入会为榜上车系创建**只有车系级信息**的存根，SKU 需 `fetch_autohome_sku.py`
单独抓。于是会出现「有销量、价格正常，但详情页 0 款型、无配置表」的车系——
2026-09-14 实测 1078 个在售车系中有 201 个处于该状态（凯美瑞、途观L、海豹06 等在列），
而汽车之家上这些车系的 SKU 都能抓到。

用法（容器内或本地均可，只需 DATABASE_URL）：

    # 覆盖率报告（缺口总量、有/无汽车之家映射的拆分、品牌类型分布）
    python tools/export_series_gaps.py --report

    # 导出待回补的汽车之家 seriesid（按销量降序，供 fetch_autohome_sku.py --ids 使用）
    python tools/export_series_gaps.py --ids --limit 25

判据口径与线上展示一致：只统计 `brand.active_status='active'` 且车系 active 的行
（否则会把 App 不展示的车系算进缺口，指标永远不收敛）。**回补前先看 `--report` 里的
`gap_without_autohome_ref`**：没有汽车之家映射的车系无法按 id 抓取，不在回补范围内。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402

GAP_WHERE = """
    s.active_status = 'active'
    and b.active_status = 'active'
    and not exists (
        select 1 from vehicle_variants v
        where v.series_id = s.id and v.status = 'on_sale'
    )
"""


def coverage(session) -> dict:
    total = session.execute(text("""
        select count(*) from vehicle_series s join brands b on b.id = s.brand_id
        where s.active_status = 'active' and b.active_status = 'active'
    """)).scalar() or 0
    gap = session.execute(text(f"""
        select count(*) from vehicle_series s join brands b on b.id = s.brand_id where {GAP_WHERE}
    """)).scalar() or 0
    gap_with_ref = session.execute(text(f"""
        select count(distinct s.id) from vehicle_series s
        join brands b on b.id = s.brand_id
        join external_series_refs r on r.series_id = s.id
        where {GAP_WHERE}
    """)).scalar() or 0
    by_type = session.execute(text(f"""
        select b.brand_type, count(*) from vehicle_series s join brands b on b.id = s.brand_id
        where {GAP_WHERE} group by b.brand_type order by count(*) desc
    """)).all()
    variants = session.execute(text("select count(*) from vehicle_variants where status='on_sale'")).scalar() or 0
    return {
        "series_total": total,
        "gap_series": gap,
        "covered_series": total - gap,
        "gap_with_autohome_ref": gap_with_ref,
        "gap_without_autohome_ref": gap - gap_with_ref,
        "on_sale_variants": variants,
        "gap_by_brand_type": {row[0]: row[1] for row in by_type},
    }


def gap_ids(session, limit: int | None = None) -> list[str]:
    """缺口车系的汽车之家 seriesid（按最高月销量降序；无映射的排在最后且被排除）。"""
    sql = f"""
        select r.external_id
        from vehicle_series s
        join brands b on b.id = s.brand_id
        join external_series_refs r on r.series_id = s.id
        where {GAP_WHERE}
        order by (select max(m.sales_count) from monthly_sales m where m.series_id = s.id) desc nulls last,
                 s.id
    """
    if limit:
        sql += f" limit {int(limit)}"
    return [row[0] for row in session.execute(text(sql)).all()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="车系款型缺口统计与导出")
    parser.add_argument("--report", action="store_true", help="打印覆盖率报告（JSON）")
    parser.add_argument("--ids", action="store_true", help="输出待回补的汽车之家 id（逗号分隔）")
    parser.add_argument("--limit", type=int, default=0, help="--ids 时最多输出多少个")
    parser.add_argument("--quiet", action="store_true", help="--ids 时只输出 id 行，不带说明")
    args = parser.parse_args(argv)

    session = get_session_factory()()
    try:
        if args.report:
            print(json.dumps(coverage(session), ensure_ascii=False, indent=1))
            return 0
        if args.ids:
            ids = gap_ids(session, args.limit or None)
            if not args.quiet:
                print(f"# 待回补 {len(ids)} 个车系（按销量降序）", file=sys.stderr)
            print(",".join(ids))
            return 0
        parser.print_help()
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
