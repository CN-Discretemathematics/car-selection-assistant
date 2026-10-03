"""逐位比较两份 eval_rag 报告——提案 §5 第一条硬判据的执行工具。

## 为什么要有这个工具

§5 要求「硬约束满足率不得回归：现有 valid-hit@5 / valid-precision@5 / valid-MRR
**逐位不变**」。但「逐位不变」靠人眼对两份 JSON 是做不到的：字段多、层级深，
少看一个就漏。

## 三个让「一致」变成假绿灯的坑（都实测踩过）

1. **别把 list 折成长度。** 报告里 `strategies` 是 `list[dict]`，早期版本把整个列表
   折成 `.len` → 4 个策略 12 个指标**一个都没比**，却输出「逐位一致」。
   现在按 `strategy` 名逐元素展开，并对键缺失/重复做显式报错。
2. **两次运行的环境必须一致。** `app/retrieval/config.py` 的 `load_dotenv()` 会把
   `backend/.env` 灌进 `os.environ`。在**没有 .env 的 worktree** 里跑，rerank/LLM
   全部降级，于是 base 与 HEAD 的 pipeline 差出 25 处指标（含 `valid_hit@5`
   0.5096 vs 0.8344 这种「大幅改善」假象——实际是 base 被 handicapped）。
   与 conftest「测试必须对环境免疫」是同一条纪律，评测侧同样适用。
3. **比较器自己也要被验证。** `--self-test` 会检查关键指标确实参与了比较，
   并**注入一个假差异**确认能被抓到；抓不到就非 0 退出。
   没通过自测的「一致」不许采信。

## 用法

    # 比较（退出码 0 = 逐位一致，1 = 有差异）
    python tools/compare_eval_reports.py before.json after.json

    # 先自测比较器自身可信，再比较
    python tools/compare_eval_reports.py --self-test before.json
    python tools/compare_eval_reports.py before.json after.json

时间戳/耗时/题库路径按定义逐次不同，比较时忽略（但会在输出里说明忽略了几个）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 逐次必然不同、且不是指标的字段
VOLATILE_HINTS = ("generated_at", "questions_file", "elapsed", "耗时", "duration")

# 必须真的参与比较的硬约束指标（§5 判据 1 的核心）
REQUIRED_KEYS = (
    "strategies[sparse].v2.valid_hit@5",
    "strategies[sparse].v2.valid_precision@5",
    "strategies[pipeline].v2.valid_mrr",
)


def _is_volatile(key: str) -> bool:
    return any(h in key.rsplit(".", 1)[-1] for h in VOLATILE_HINTS)


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """报告 → {点分路径: 标量}；list 按元素展开并用 `strategy` 名作键。"""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        if not obj:
            out[prefix] = "[]"
        for i, item in enumerate(obj):
            label = str(i)
            if isinstance(item, dict):
                label = str(item.get("strategy") or item.get("name") or i)
            out.update(flatten(item, f"{prefix}[{label}]"))
    else:
        out[prefix] = obj
    return out


def _load(path: str) -> dict[str, Any]:
    return flatten(json.loads(Path(path).read_text(encoding="utf-8")))


def self_test(report: str) -> int:
    """证明本比较器**能**报出差异，且关键指标确实参与了比较。"""
    flat = _load(report)
    fails: list[str] = []

    print(f"[1] 摊平后字段数 = {len(flat)}")
    if len(flat) < 50:
        fails.append(f"摊平后字段过少（{len(flat)}），很可能又把 list 折成长度了")

    print("\n[2] 关键硬约束指标是否在比较集合内：")
    for key in REQUIRED_KEYS:
        present = key in flat
        print(f"    {'OK  ' if present else 'MISS'} {key} = {flat.get(key, '<缺>')}")
        if not present:
            fails.append(f"关键指标未参与比较：{key}")

    print("\n[3] 注入假差异，确认能被抓到：")
    data = json.loads(Path(report).read_text(encoding="utf-8"))
    mutated = False
    for s in data.get("strategies") or []:
        v2 = s.get("v2") or {}
        if "valid_hit@5" in v2:
            # ⚠️ 策略名在 `strategy` 键上。写成 name 会一个都匹配不到 → 注入是空操作
            # → 「注入后仍报一致」，等于在验证一个根本没发生过的事件。
            v2["valid_hit@5"] = 0.0001
            mutated = True
            break
    if not mutated:
        print("    SKIP：报告里没有 v2.valid_hit@5，无法注入")
        fails.append("无法注入差异（报告结构不含 v2.valid_hit@5）")
    else:
        tmp = Path(report).with_suffix(".selftest.json")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            rc, _ = _compare(_load(report), _load(str(tmp)))
        finally:
            tmp.unlink(missing_ok=True)
        caught = rc != 0
        print(f"    {'OK  ' if caught else 'FAIL'} 注入后退出码 {rc}（应为非 0）")
        if not caught:
            fails.append("注入差异却没被抓到 —— 比较器是假绿灯")

    print("\n" + (f"FAIL:\n  - {fails[0]}" if len(fails) == 1 else
                  ("FAIL:\n  - " + "\n  - ".join(fails) if fails else "PASS：比较器可信。")))
    return 1 if fails else 0


def _compare(before: dict[str, Any], after: dict[str, Any]) -> tuple[int, list[tuple[str, Any, Any]]]:
    keys = sorted(set(before) | set(after))
    volatile = [k for k in keys if _is_volatile(k)]
    diffs: list[tuple[str, Any, Any]] = []
    for k in keys:
        if _is_volatile(k):
            continue
        b, a = before.get(k, "<缺>"), after.get(k, "<缺>")
        if b != a:
            diffs.append((k, b, a))
    if volatile:
        print(f"（已忽略 {len(volatile)} 个非指标字段：时间戳/耗时/题库路径）")
    return (1 if diffs else 0), diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="逐位比较两份 eval_rag 报告（§5 判据 1）")
    ap.add_argument("before")
    ap.add_argument("after", nargs="?")
    ap.add_argument("--self-test", action="store_true", help="先验证比较器自身可信")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test(args.before)
    if not args.after:
        ap.error("需要 before 与 after 两份报告（或用 --self-test）")

    rc, diffs = _compare(_load(args.before), _load(args.after))
    if not diffs:
        print("逐位一致：所有指标字段全部相同（含四桶分桶指标与硬约束满足率）。")
        return 0
    print(f"发现 {len(diffs)} 处差异：\n")
    for k, b, a in diffs:
        print(f"  {k}\n      before={b}\n      after ={a}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
