"""评测题库锚点漂移：检测与增量修复。

## 背景（2026-10-20 实测）

`backend/eval/questions.json` 的 `anchors.series_id` / `variant_id` 是**数字 id**，
而题面 `text` 里的**车系名 / 款型名是稳定字符串**。库重新导入后 id 被重排，
于是 anchors 指向的车与题面说的**不是同一台车**。

本地 94MB 生产快照（908 车系 / 5132 在售款型）实测：

    anchors 与题面一致:        4 / 410
    anchors 存在但指向别的车:  399
    anchors 在库里不存在:        7

**这不是解析器的问题。** 决定性对照——用题面文字跑生产解析器：

    q0007 题面「路虎卫士」   → 解析出 路虎卫士 ✓   anchors 指向 传祺E8 PHEV ✗
    q0019 题面「领克08 EM-P」→ 解析出 领克08 EM-P ✓ anchors 指向 传祺M8 ✗
    q0061 题面「英菲尼迪QX50」→ 解析出 英菲尼迪QX50 ✓ anchors 指向 仰望U7 ✗

**解析器是对的，anchors 是错的。** 所以修法不是「让解析器更准」，
而是**用题面文字把 anchors 重新定位**——题面里就有真相。

## 能修什么，不能修什么

| 题面形态 | 题数 | 能否修 |
| --- | --- | --- |
| 含**车系名**或**款型名** | 250 | ✅ 用生产解析器反查 id |
| 只有「预算/用途/人数/车身」等约束 | 156 | ❌ 无实体标识，锚点是生成时的随机选择 |
| semantic 桶（本无实体锚） | 110 | — 不动 |

156 道不可重定位的题**必须重跑生成器**（`gen_eval_questions.py`，固定种子）。
本工具**只做增量修**，不重跑、不改题面。

## 用法

    # 检测（不改文件）；有漂移时退出码 1，可接 CI
    python tools/fix_eval_anchors.py --check

    # 增量修复（先备份再写回）
    python tools/fix_eval_anchors.py --fix

    # 指定题库路径
    python tools/fix_eval_anchors.py --check --questions eval/questions.json

需要 `DATABASE_URL` 指向**当前生产库**（不是历史快照）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.series_index import resolve_series_with_dropped
from app.common.database import get_session_factory
from app.common.models import VehicleSeries, VehicleVariant

DEFAULT_QUESTIONS = "eval/questions.json"


# ── 库内实体的稳定标识 ──────────────────────────────────────────────────────


def load_entities(session: Session) -> tuple[
    dict[int, str], dict[int, tuple[int, str, str]]
]:
    """返回 (series_id → 车系名, variant_id → (series_id, display_name, config_version))。

    ## 两个款型名字段的分工

    生产库里（实测 5132 在售款型）：

        config_version = '310km 向往版'          纯配置名，**143 个**跨车系共享
        display_name   = '2026款 310km 向往版'    带年款，**89 个**跨车系共享

    - `display_name` 才是**题面里出现的形态**（「2026款 Ultra」「2026款 100kWh」）；
    - `config_version` 是**共享面更大**的纯配置名，做撞名统计时用它更准
      （「2026款」这层年款会掩盖「310km 向往版」其实被 3 个车系共用的事实）。
    """
    series_names = dict(
        session.execute(select(VehicleSeries.id, VehicleSeries.name)).all()
    )
    variants = {
        vid: (sid, (disp or "").strip(), (cfg or "").strip())
        for vid, sid, cfg, disp in session.execute(
            select(
                VehicleVariant.id,
                VehicleVariant.series_id,
                VehicleVariant.config_version,
                VehicleVariant.display_name,
            )
        ).all()
    }
    return series_names, variants


def has_stable_identifier(text: str, series_names: dict[int, str]) -> bool:
    """题面里有没有**解析器认得的稳定标识**（车系名 / 别名 / 短名）。

    ## 为什么只认车系名，不认款型名

    生产解析器（`series_index._load_name_entries`，实测 7068 条）只索引
    **车系名 / 品牌+车系名 / 别名 / 短名**——**款型显示名不在索引里**。

        共享款型名 '2026款 后驱标准版'  → 解析出 0 个，索引里也没有它

    所以款型名**不能**用来重定位锚点。把它算作「可重定位」会给出假阳性：
    那种题实际会被解析器判成「解析不出」，属 `unresolved` 而非 `unlocatable`。

    （实测 520 道题里「只含款型名」的题是 **0 道**——款型名总是伴随车系名出现，
    或者两者都没有。所以这个判据收紧在当前数据上不改变结果，但**判据本身要对**：
    将来题库里若出现只写款型名的题，它会被正确地报成「不可重定位」。）
    """
    return any(name and name in text for name in series_names.values())


# ── 单题分析 ──────────────────────────────────────────────────────────────


def analyze_question(
    session: Session,
    question: dict,
    series_names: dict[int, str],
    variants: dict[int, tuple[int, str, str]],
) -> dict:
    """分析一道题的锚点状态，返回 {status, ...}。

    status 取值：
        ok              锚点与题面一致，无需动
        stale           锚点漂移，可用题面重定位
        unlocatable     锚点漂移，但题面无实体标识，**不能修**
        no_anchor       本就无实体锚（semantic 桶），不动
        ambiguous       题面能定位到多个车系，需要人工判断
        unresolved      题面解析不出任何车系
    """
    text = question.get("text") or ""
    anchors = question.get("anchors") or {}
    sid = anchors.get("series_id")
    vid = anchors.get("variant_id")

    if sid is None and vid is None:
        return {"status": "no_anchor"}

    # 当前锚点是否已漂移
    drifted = False
    if sid is not None:
        current = series_names.get(sid)
        if current is None:
            drifted = True  # id 在库里不存在
        elif current not in text:
            drifted = True  # id 存在但指向别的车
    else:
        drifted = True  # 有 variant_id 但没有 series_id

    if not drifted:
        return {"status": "ok", "series_id": sid, "variant_id": vid}

    if not has_stable_identifier(text, series_names):
        return {"status": "unlocatable", "series_id": sid, "variant_id": vid}

    # 用生产解析器在题面文字上重定位
    resolved, _dropped = resolve_series_with_dropped(session, text)
    if not resolved:
        return {"status": "unresolved", "series_id": sid, "variant_id": vid}
    if len(resolved) > 1:
        return {
            "status": "ambiguous",
            "series_id": sid,
            "variant_id": vid,
            "candidates": [f"{s.name}({s.id})" for s, _b in resolved],
        }

    new_series = resolved[0][0]
    new_variant = None
    if vid is not None:
        # 款型 id：若它原本属于别的车系，则该 id 同样失效——
        # 只能靠「款型名 + 车系」重新找。找不到就只修 series_id。
        cur = variants.get(vid)
        cur_sid = cur[0] if cur else None
        if cur_sid == new_series.id:
            new_variant = vid  # 款型 id 没漂移
        else:
            for cand_vid, (cand_sid, cand_disp, _cand_cfg) in variants.items():
                if cand_sid == new_series.id and cand_disp and cand_disp in text:
                    new_variant = cand_vid
                    break

    return {
        "status": "stale",
        "series_id": sid,
        "variant_id": vid,
        "new_series_id": new_series.id,
        "new_variant_id": new_variant,
        "new_series_name": new_series.name,
    }


# ── 批量分析 ──────────────────────────────────────────────────────────────


def analyze_all(
    session: Session, questions: list[dict]
) -> tuple[list[dict], Counter]:
    series_names, variants = load_entities(session)

    results = []
    stats: Counter = Counter()
    for q in questions:
        res = analyze_question(session, q, series_names, variants)
        res["id"] = q.get("id")
        res["bucket"] = q.get("bucket")
        results.append(res)
        stats[res["status"]] += 1
    return results, stats


def apply_fixes(
    questions: list[dict], results: list[dict]
) -> tuple[int, int]:
    """把可修的题的 anchors 就地改正。返回 (修好的题数, 改动的 id 数)。"""
    by_id = {r["id"]: r for r in results}
    fixed = 0
    ids_changed = 0
    for q in questions:
        res = by_id.get(q.get("id"))
        if not res or res["status"] != "stale":
            continue
        anchors = q.setdefault("anchors", {})
        changed = False
        if anchors.get("series_id") != res["new_series_id"]:
            anchors["series_id"] = res["new_series_id"]
            changed = True
        if res.get("new_variant_id") is not None and anchors.get("variant_id") != res["new_variant_id"]:
            anchors["variant_id"] = res["new_variant_id"]
            changed = True
        if changed:
            fixed += 1
            ids_changed += 1
    return fixed, ids_changed


# ── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None, session: Session | None = None) -> int:
    """CLI 入口。

    `session` 参数只为**自测**存在：测试要跑在夹具库上，不能自己开一个连到别处的
    session。生产调用不传。
    """
    parser = argparse.ArgumentParser(
        description="评测题库锚点漂移：检测与增量修复（用题面文字重定位 id）"
    )
    parser.add_argument("--check", action="store_true", help="只检测并报告，不改文件")
    parser.add_argument("--fix", action="store_true", help="增量修复并写回（先备份）")
    parser.add_argument("--questions", default=DEFAULT_QUESTIONS, help="题库路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    parser.add_argument("--limit", type=int, default=0, help="明细最多显示多少条")
    args = parser.parse_args(argv)

    if not args.check and not args.fix:
        parser.print_help()
        return 0

    qpath = Path(args.questions)
    if not qpath.exists():
        print(f"找不到题库：{qpath}", file=sys.stderr)
        return 2

    payload = json.loads(qpath.read_text(encoding="utf-8"))
    questions = payload["questions"] if isinstance(payload, dict) else payload

    own_session = session is None
    session = session or get_session_factory()()
    try:
        results, stats = analyze_all(session, questions)
    finally:
        if own_session:
            session.close()

    total = len(questions)
    report = {
        "total": total,
        "by_status": dict(stats),
        "drifted": stats["stale"] + stats["unlocatable"] + stats["ambiguous"] + stats["unresolved"],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"题库：{qpath}（{total} 题）")
        for k, v in sorted(stats.items()):
            print(f"  {k}: {v}")
        if report["drifted"]:
            print(f"\n⚠️ 锚点漂移 {report['drifted']} 题"
                  f"（可增量修 {stats['stale']}，需重跑生成器 {stats['unlocatable']}）")

    if args.fix:
        fixed, ids_changed = apply_fixes(questions, results)
        backup = qpath.with_suffix(qpath.suffix + ".bak")
        shutil.copy2(qpath, backup)
        qpath.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"\n已修 {fixed} 题（改动 {ids_changed} 个 id），备份：{backup}")

    if args.check and report["drifted"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
