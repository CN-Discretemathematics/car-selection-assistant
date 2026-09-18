# -*- coding: utf-8 -*-
"""Shadow 对拍报告聚合（Agent 理解层智能化 W0 Phase 2）。

输入：logger "app.agent.router.shadow" 的单行 JSON 记录列表（stdin 或 --file）；
输出：markdown 报告（stdout 或 --out 路径）。报告写到 backend/eval/ 属
gitignored 本地产物；本脚本在包内入库，运行方式：

    python -m app.agent.shadow_report --file shadow.log --out ../eval/shadow-report.md

聚合口径（与 llm_router.log_shadow_record 的字段一一对应）：
- 「有裁决」= llm_intent 非空的记录；llm_intent 为 null（超时/异常/输出不合法）
  单列为「无裁决」，**不进一致率分母**——一致率衡量的是两个路由器的意见分歧，
  不是 LLM 可用性；
- 一致率 = agree 记录数 / 有裁决记录数（agree 与 regex_intent==llm_intent 等价）；
- LLM 耗时 p50/p95 只统计 llm_elapsed_ms 为数值的记录（含无裁决记录——那次
  等待真实发生过，正是延迟分析要的数据）。

切流判据用法（判据写死在 app/agent/llm_router.py 模块 docstring）：本报告产出
一致率与分歧清单；「LLM 对、regex 错 ≥ 分歧量 80%」需要人工标注分歧清单后
另行计算，本工具只负责把分歧清单完整、可读地摆出来。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from app.agent.llm_router import SHADOW_LOGGER


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """线性插值百分位；入参必须已升序。空列表返回 None。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(float(sorted_values[0]), 2)
    rank = (len(sorted_values) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return round(float(sorted_values[low]) * (1 - frac) + float(sorted_values[high]) * frac, 2)


def _parse_rows(lines: Iterable[Any]) -> list[dict]:
    """容忍脏行：空行 / 非 JSON / 非对象一律跳过（日志可能有截断）。

    生产日志带 logging formatter 前缀（时间/级别/logger 名），形如
    `2026-09-18 13:39:18,051 INFO app.agent.router.shadow {"utterance": ...}`；
    docker logs 还会再加容器前缀。裸 JSON 解析失败时：若行内含 shadow logger 名
    则截取首尾大括号之间的内容再试一次（评审三轮 B3：否则 `--file shadow.log`
    产出空报告且无从分辨）；不含标记的行（如回答级耗时日志）不误收。
    """
    rows: list[dict] = []
    for raw in lines:
        if isinstance(raw, dict):
            rows.append(raw)
            continue
        text = str(raw).strip()
        if not text:
            continue
        value = _loads_object(text)
        if value is None and SHADOW_LOGGER in text:
            start, end = text.find("{"), text.rfind("}")
            if 0 <= start < end:
                value = _loads_object(text[start:end + 1])
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _loads_object(text: str) -> Any:
    """只做直接解析；大括号截取兜底在 _parse_rows 里由 logger 名标记守门。"""
    try:
        return json.loads(text)
    except ValueError:
        return None


def aggregate(lines: Iterable[Any]) -> dict:
    """shadow 单行 JSON 记录列表 → 对拍聚合结果（一致率、分歧清单、LLM 耗时分位）。

    `shadow_skipped`（旁路 in-flight 满被丢弃的采样）单列 `skipped`，不计入
    total/judged/no_verdict——丢弃不是「LLM 无裁决」，混入会让切流期的
    可用性口径失真（评审四轮建议 2）。
    """
    rows = _parse_rows(lines)
    skipped = 0
    judged = 0
    no_verdict = 0
    agreed = 0
    disagreements: list[dict] = []
    pair_counter: Counter = Counter()
    regex_counter: Counter = Counter()
    llm_counter: Counter = Counter()
    elapsed_values: list[float] = []

    kept: list[dict] = []
    for row in rows:
        if row.get("shadow_skipped"):
            skipped += 1
            continue
        kept.append(row)
    total = len(kept)

    for row in kept:
        elapsed = row.get("llm_elapsed_ms")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            elapsed_values.append(float(elapsed))
        regex_intent = row.get("regex_intent")
        regex_counter[regex_intent if isinstance(regex_intent, str) else "-"] += 1
        llm_intent = row.get("llm_intent")
        if not llm_intent:
            no_verdict += 1
            continue
        judged += 1
        llm_counter[str(llm_intent)] += 1
        if llm_intent == regex_intent:
            agreed += 1
        else:
            pair_counter[(regex_intent, llm_intent)] += 1
            disagreements.append(
                {
                    "utterance": str(row.get("utterance", "")),
                    "regex_intent": regex_intent,
                    "llm_intent": llm_intent,
                    "llm_confidence": row.get("llm_confidence"),
                }
            )

    elapsed_values.sort()
    return {
        "total": total,
        "skipped": skipped,
        "judged": judged,
        "no_verdict": no_verdict,
        "agreed": agreed,
        "agreement_rate": round(agreed / judged, 4) if judged else None,
        "disagreement_count": len(disagreements),
        "disagreements": disagreements,
        "confusion_pairs": [
            {"regex_intent": regex_intent, "llm_intent": llm_intent, "count": count}
            for (regex_intent, llm_intent), count in pair_counter.most_common()
        ],
        "llm_elapsed_ms": {
            "count": len(elapsed_values),
            "p50": _percentile(elapsed_values, 50),
            "p95": _percentile(elapsed_values, 95),
        },
        "intent_distribution": {
            "regex": dict(regex_counter.most_common()),
            "llm": dict(llm_counter.most_common()),
        },
    }


def render_markdown(report: dict) -> str:
    """聚合结果 → markdown 报告。"""
    elapsed = report.get("llm_elapsed_ms") or {}
    p50 = elapsed.get("p50")
    p95 = elapsed.get("p95")
    rate = report.get("agreement_rate")
    lines = [
        "# Shadow 路由对拍报告",
        "",
        f"- 记录总数：{report.get('total', 0)}"
        f"（LLM 无裁决 {report.get('no_verdict', 0)} 条——超时/异常/输出不合法，不进一致率分母；"
        f"旁路丢弃 {report.get('skipped', 0)} 条——in-flight 满，单列不计入）",
        "- 一致率："
        + (
            f"{rate:.2%}（一致 {report.get('agreed', 0)} / 有裁决 {report.get('judged', 0)}）"
            if rate is not None
            else "无有裁决记录"
        ),
        "- LLM 路由耗时："
        + (
            f"p50 {p50} ms / p95 {p95} ms（{elapsed.get('count', 0)} 条有耗时记录）"
            + (
                "；样本不足 20 条，p95 仅供参考、不作为切流判据 4 的依据"
                if elapsed.get("count", 0) < 20
                else ""
            )
            if p50 is not None
            else "无有耗时记录"
        ),
        "",
        "## 意图分布",
        "",
        "| intent | regex | llm |",
        "|---|---|---|",
    ]
    regex_dist = (report.get("intent_distribution") or {}).get("regex") or {}
    llm_dist = (report.get("intent_distribution") or {}).get("llm") or {}
    intents = sorted((set(regex_dist) | set(llm_dist)) - {""})
    for intent in intents:
        lines.append(f"| {intent} | {regex_dist.get(intent, 0)} | {llm_dist.get(intent, 0)} |")
    lines += [
        "",
        f"## 分歧清单（{report.get('disagreement_count', 0)} 条）",
        "",
        "「LLM 对、regex 错 ≥ 分歧量 80%」需人工标注本清单后计算（判据见 llm_router.py docstring）。",
        "",
        "| # | utterance | regex_intent | llm_intent | llm_confidence |",
        "|---|---|---|---|---|",
    ]
    for i, item in enumerate(report.get("disagreements") or [], start=1):
        lines.append(
            f"| {i} | {str(item.get('utterance', '')).replace('|', '\\|')} "
            f"| {item.get('regex_intent')} | {item.get('llm_intent')} | {item.get('llm_confidence')} |"
        )
    lines += [
        "",
        "## 高频分歧对",
        "",
        "| regex_intent | llm_intent | count |",
        "|---|---|---|",
    ]
    pairs = report.get("confusion_pairs") or []
    if pairs:
        for pair in pairs:
            lines.append(
                f"| {pair.get('regex_intent')} | {pair.get('llm_intent')} | {pair.get('count')} |"
            )
    else:
        lines.append("| （无分歧） | - | 0 |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="shadow 路由对拍报告（app.agent.router.shadow 日志 → markdown）")
    parser.add_argument("--file", help="shadow 单行 JSON 日志文件路径（缺省读 stdin）")
    parser.add_argument("--out", help="markdown 输出文件路径（缺省写 stdout）")
    args = parser.parse_args(argv)

    if args.file:
        lines: list[str] = Path(args.file).read_text(encoding="utf-8").splitlines()
    else:
        # Windows 控制台管道默认非 UTF-8：显式重配，避免中文日志读成乱码
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        lines = sys.stdin.read().splitlines()

    markdown = render_markdown(aggregate(lines))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
