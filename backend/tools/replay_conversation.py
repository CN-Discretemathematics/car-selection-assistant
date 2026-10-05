"""从 `agent_turn_logs` 导出一次会话，用于**离线重放**。

为什么需要这个脚本（2026-10-05）：留档表存下来了不等于能复现。真正排障时要回答
的是「那一轮在本地重放，intent / matched_rule / series_ids 是否一致」——
这个比对得能导出成机器可读的输入。人工从 DB 里捞 JSON 既慢又容易抄错。

用法（在 `backend/` 下）：

    python tools/replay_conversation.py <session_id>            # 打印该会话全部轮次
    python tools/replay_conversation.py <session_id> --json     # 机器可读（喂回放脚本）
    python tools/replay_conversation.py <session_id> --check    # 只看 intent/车系摘要

数据库取 `DATABASE_URL`（默认 `sqlite:///./dev.db`）。

⚠️ 输出里是**已脱敏**的文本（手机号/邮箱/长数字），但仍包含用户的原始问法。
导出物按内部排障材料对待，不要贴到公开 issue 或聊天群里。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys

sys.path.insert(0, ".")

# Windows 控制台默认 GBK，打印中文会 UnicodeEncodeError（2026-10-05 实测踩到）。
# 排障脚本因为中文读不出来而没法用，等于没有——所以在这里强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # 老 Python / 已重定向到二进制
        _stream.reconfigure(encoding="utf-8")

from app.agent.conversation_log import load_session  # noqa: E402
from app.common.database import get_session_factory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导出一段对话留档用于离线重放")
    parser.add_argument("session_id", help="会话 id")
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    parser.add_argument("--check", action="store_true", help="只输出 intent / 车系摘要")
    args = parser.parse_args()

    db = get_session_factory()()
    try:
        turns = load_session(db, args.session_id)
    finally:
        db.close()

    if not turns:
        print(f"没有找到会话 {args.session_id} 的留档（已过期？功能未开？）", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(turns, ensure_ascii=False, indent=2))
        return 0

    if args.check:
        for row in turns:
            print(f"[{row['turn_index']}] intent={row['intent']:<16} "
                  f"rule={row['matched_rule']} series={row['series_ids']}")
            print(f"      用户：{row['user_text']}")
        return 0

    for row in turns:
        print(f"=== 第 {row['turn_index']} 轮 @ {row['created_at']} ===")
        print(f"用户：{row['user_text']}")
        print(f"助手：{row['reply_text']}")
        print(f"路由：intent={row['intent']}  rule={row['matched_rule']}")
        print(f"车系：{row['series_ids']}")
        if row["signals"]:
            print(f"信号：{json.dumps(row['signals'], ensure_ascii=False)}")
        if row["profile_snapshot"]:
            print(f"画像：{json.dumps(row['profile_snapshot'], ensure_ascii=False)}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
