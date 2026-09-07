"""月度销量数据自动获取（每日由 systemd timer / Windows 计划任务触发）。

策略（幂等，任意时刻重跑安全）：
1. 目标月 = 最近完整自然月（每月 1 号起目标即「上月」）；
2. 库中已有目标月销量数据 → 已就绪，退出 0，不再抓取；
3. 否则抓取汽车之家目标月榜单：
   - 门户已发布目标月 → 导入 → 校验库内目标月行数 → 成功，退出 0；
   - 门户仍公布上一月（数据未发布）→ 不导入，日志「未就绪」，次日自动再试，
     直到目标月数据到位（即「1 号拿不到 2 号再试」的每日重试）；
4. 未就绪期间，首页/详情自动展示库内最新数据月并如实标注月份
   （app.catalog.latest_sales_month），无需人工干预；
5. 未就绪持续 ≥ --warn-days（默认 15 天）时写告警日志，提醒人工检查数据源。

用法：
    python tools/fetch_sales_scheduled.py [--force] [--month YYYY-MM]
                                          [--dry-run] [--warn-days N]

退出码：0 = 成功或未就绪（预期状态）；2 = 网络/数据库/导入错误（需人工关注）。
日志：stdout（journald / 计划任务捕获）+ logs/sales_fetch.log（追加）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.catalog.services import latest_full_month  # noqa: E402
from app.common.database import get_session_factory  # noqa: E402
from app.common.models import MonthlySales  # noqa: E402
from tools.fetch_autohome_sales import fetch_and_import_month  # noqa: E402

DEFAULT_LOG_PATH = os.path.join("logs", "sales_fetch.log")
DEFAULT_WARN_DAYS = 15


def sales_month_count(db, month: str) -> int:
    """指定月份库内销量行数（零售/门户口径，与首页一致）。"""
    return (
        db.scalar(
            select(func.count())
            .select_from(MonthlySales)
            .where(
                MonthlySales.month == month,
                MonthlySales.sales_type.in_(("retail", "portal")),
            )
        )
        or 0
    )


def days_since_month_end(month: str, today: date | None = None) -> int:
    """目标月结束至今的天数（月份尚未结束为负；用于滞后告警）。"""
    year, mon = (int(part) for part in month.split("-"))
    next_first = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    last_day = next_first - timedelta(days=1)
    today = today or date.today()
    return (today - last_day).days


def _logger(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def log(msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:  # 日志目录不可写时不阻塞主流程
            pass

    return log


def main(argv: list[str] | None = None, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description="月度销量数据自动获取（每日触发，幂等）")
    parser.add_argument("--force", action="store_true", help="即使库内已有目标月数据也重新抓取")
    parser.add_argument("--month", default=None, help="YYYY-MM；默认最近完整自然月")
    parser.add_argument("--dry-run", action="store_true", help="抓取校验但不导入")
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS, help="未就绪超过 N 天告警")
    parser.add_argument("--log", default=DEFAULT_LOG_PATH, help="日志文件路径")
    args = parser.parse_args(argv)

    if args.month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month):
        print(f"失败：--month 格式错误（应为 YYYY-MM）：{args.month}", file=sys.stderr)
        return 2  # 评审 P2：此前非法月份会在查询处裸 traceback

    month = args.month or latest_full_month()
    log = _logger(args.log)
    factory = session_factory or get_session_factory()

    try:
        with factory() as db:
            present = sales_month_count(db, month)
    except Exception as err:  # noqa: BLE001
        log(f"失败：数据库连接异常 {type(err).__name__}: {err}")
        return 2

    if present and not args.force:
        log(f"已就绪：目标月 {month} 已有销量数据（{present} 行），无需抓取")
        return 0

    log(f"目标月 {month} 尚无数据（库内 {present} 行），尝试抓取…")
    try:
        report = fetch_and_import_month(
            month,
            do_import=not args.dry_run,
            dry_run=args.dry_run,
            require_month_match=True,
        )
    except PermissionError as err:
        log(f"失败：robots 政策拒绝 {err}（请人工确认条款）")
        return 2
    except Exception as err:  # noqa: BLE001
        log(f"失败：抓取异常 {type(err).__name__}: {err}（明日自动重试）")
        return 2

    if report["skipped_reason"]:
        days = days_since_month_end(month)
        log(f"未就绪：{report['skipped_reason']}（已滞后 {days} 天，明日自动重试）")
        if days >= args.warn_days:
            log(f"告警：{month} 销量数据已滞后 {days} 天，请人工检查数据源/门户发布情况")
        return 0

    if args.dry_run:
        log(f"dry-run：{month} 榜单已发布（{report['rows']} 行），未导入")
        return 0

    if report.get("errors"):
        log(f"失败：导入报告 {len(report['errors'])} 条错误：{'；'.join(report['errors'][:5])}")
        return 2

    try:
        with factory() as db:
            after = sales_month_count(db, month)
    except Exception as err:  # noqa: BLE001
        log(f"失败：导入后校验查询异常 {type(err).__name__}: {err}")
        return 2
    if after == 0:
        log("失败：导入完成但库内目标月仍无数据行，请人工检查 importer 日志")
        return 2
    log(f"成功：{month} 数据到位，库内 {after} 行（新建 {report['created']}，更新 {report['updated']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
