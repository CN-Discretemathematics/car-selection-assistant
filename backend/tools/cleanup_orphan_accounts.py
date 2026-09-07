"""孤儿账号清理（评审 M-L5-1）：登录即注册产生的、从未使用的账号。

「孤儿」定义：没有任何收藏，且不拥有任何对比（comparisons.owner_id = user.id）。
默认 dry-run 只统计不删除；--apply 才实际删除，且可 --min-age-days 限制只清理
创建较早的账号，避免误删「刚登录、还没操作」的用户。

用法：python tools/cleanup_orphan_accounts.py [--apply] [--min-age-days N]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, select  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import Comparison, Favorite, User  # noqa: E402


def _mask(email: str | None, phone: str | None) -> str:
    if email:
        local, _, domain = email.partition("@")
        return f"{local[:1]}***@{domain}" if local else email
    if phone:
        return f"{phone[:3]}****{phone[-2:]}"
    return "（无账号标识）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="孤儿账号清理（默认 dry-run 只统计）")
    parser.add_argument("--apply", action="store_true", help="实际删除孤儿账号")
    parser.add_argument("--min-age-days", type=int, default=30, help="只清理创建超过 N 天的孤儿账号")
    args = parser.parse_args(argv)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.min_age_days)
    orphan_cond = (
        ~User.id.in_(select(Favorite.user_id))
        & ~User.id.in_(
            select(Comparison.owner_id).where(Comparison.owner_id.isnot(None))
        )
        & (User.created_at < cutoff)
    )

    factory = get_session_factory()
    with factory() as db:
        total = db.scalar(select(func.count()).select_from(User)) or 0
        orphans = db.scalar(
            select(func.count()).select_from(User).where(orphan_cond)
        ) or 0
        samples = db.execute(
            select(User.email, User.phone, User.created_at)
            .where(orphan_cond)
            .order_by(User.created_at)
            .limit(5)
        ).all()

    print(f"账号总数：{total}")
    print(f"孤儿账号（无收藏且无对比，创建早于 {args.min_age_days} 天）：{orphans}")
    if samples:
        print("示例（已脱敏）：")
        for email, phone, created_at in samples:
            print(f"  {_mask(email, phone)} · 创建于 {created_at.strftime('%Y-%m-%d') if created_at else '未知'}")

    if orphans and not args.apply:
        print("dry-run：未删除。确认后加 --apply 执行。")
        return 0
    if orphans and args.apply:
        with factory() as db:
            result = db.execute(delete(User).where(orphan_cond))
            db.commit()
        print(f"已删除 {result.rowcount} 个孤儿账号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
