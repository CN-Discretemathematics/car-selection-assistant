# -*- coding: utf-8 -*-
"""Soft 偏好 shadow 报告聚合（`app.agent.soft_prefs.shadow` 日志 → markdown）。

输入：logger `app.agent.soft_prefs.shadow` 的单行 JSON 记录（stdin 或 `--file`）；
输出：markdown 报告（stdout 或 `--out`）。运行方式：

    python -m app.agent.soft_prefs_report --file soft-prefs.log --out ../eval/soft-prefs.md

## 为什么要有这个工具

切流判据（`app/agent/soft_prefs.py` 模块 docstring）写着「越界率必须恒为 0」，
但那个指标此前**在运行时根本测不出来**：`_pick_value` 把越界值静默丢弃，
`log_shadow` 落的 `prefs` 是 sanitize 之后的输出，
`tools/eval_soft_prefs.py` 统计的 `illegal_values` 取的也是 sanitize 后输出——
**必然为 0**，是构造性的、不是证据。

观测字段（`illegal` / `status`）补齐之后，还差最后一步：把日志统计成**能判定的数字**。
本工具就是这一步。没有它，判据仍然只能靠人 grep 日志。

## 三个「不许假绿」的硬规矩

1. **越界率的分母只算 LLM 真的响应过的记录**（`status` ∈ {`ok`, `all_rejected`}）。
   若把 `no_response`（超时/异常/空内容/客户端不可用）也算进分母，
   **一次大面积超时就会让越界率显示为 0**——不是因为模型守规矩，
   而是因为它根本没被调用上。
2. **`unknown` 非零即判不可判定**。`status=unknown` 表示这条路径没有埋点或
   埋了不止一次；它既可能是「没问题」，也可能是「没测到」，**不能默认按干净算**。
3. **样本强度单列**。`no_response` 占比过高时，越界率再低也只是「样本不可信」，
   报告必须写明而不是给一个漂亮的 0。

跨版本切分：按记录里的 `version`（= `soft_prefs.SOFT_PREF_VERSION`）分组——
换提示词或换词表来源时必须递增，否则新旧数据混在一起算出的越界率没有意义
（同 `llm_router.ROUTER_VERSION` 的理由）。
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from app.agent.soft_prefs import SHADOW_LOGGER, STATUSES

#: LLM 真的响应过的状态——越界率的分母只用这两个
_RESPONDED = ("ok", "all_rejected")

#: 样本强度下限：低于这个量，越界率只作参考（对齐 shadow_report 对 p95 的口径）
_MIN_SAMPLE = 20

#: 空偏好率上限：超过它，「越界率 0」没有意义——模型每条都在说废话却从不越界，
#: 说明它没在按 schema 思考。审查实测：25 条全 `all_rejected` + `illegal=[]`
#: 会得到「通过」，那是假绿。
_MAX_EMPTY_RATE = 0.5

#: `illegal` 键缺失时的说明文案（该字段 2026-10-04 才上线）
_ILLEGAL_FIELD_HINT = "该字段 2026-10-04 才上线；请核对 SOFT_PREF_VERSION 是否递增"

#: 被丢弃的脏行计数。用单元素列表当 out-param，让 `_parse_rows`（纯函数风格）
#: 不必改返回值形状就能把它带出来。
_skipped = [0]

#: 自验证样本的 message_head 序号器
_st_seq = itertools.count()


def _loads_object(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _is_shadow_row(value: Any) -> bool:
    """是不是一条 soft_prefs shadow 记录——**必须**校验，裸 JSON 不算数。

    踩过的坑：初版只对「括号截取兜底」那一步查 logger 名，**裸 JSON 行无条件接受**。
    结果 `app.agent.router.shadow` 的记录、以及 `app.agent.respond` 那些 JSON
    回答级耗时日志，全都被算进抽取质量统计——分母直接被污染，而报告看起来完全正常。

    因此这里按记录自带的 `logger` 字段守门：认不出来的一律**丢**，不猜。
    """
    return isinstance(value, dict) and value.get("logger") == SHADOW_LOGGER


def _parse_rows(lines: Iterable[Any]) -> list[dict]:
    """容忍脏行：空行 / 非 JSON / 非 shadow 记录一律跳过（日志可能被截断）。

    生产日志带 logging formatter 前缀（时间/级别/logger 名），docker logs 还会
    再加容器前缀。故裸 JSON 解析失败时：若行内含 shadow logger 名，则截取
    首尾大括号之间的内容再试一次。
    """
    rows: list[dict] = []
    skipped = 0
    for raw in lines:
        if isinstance(raw, dict):
            if _is_shadow_row(raw):
                rows.append(raw)
            else:
                skipped += 1
            continue
        text = str(raw).strip()
        if not text:
            continue
        value = _loads_object(text)
        if value is None and SHADOW_LOGGER in text:
            start, end = text.find("{"), text.rfind("}")
            if 0 <= start < end:
                value = _loads_object(text[start:end + 1])
        if _is_shadow_row(value):
            rows.append(value)
        else:
            # **必须计数**：否则「多记录挤一行被截断」「日志文件是垃圾」都会
            # 静默蒸发，`total=0` 与「日志本来就是空的」长得一模一样
            # （审查建议 7；对标 shadow_report 的 skipped）。
            skipped += 1
    _skipped[0] += skipped
    return rows


def _illegal_entries(row: dict) -> tuple[list[dict] | None, str]:
    """取该记录的封闭失效条目，并**区分三种「取不到」**。

    返回 `(entries, problem)`，`problem` 取值：

    - `""`：正常，取到了（可能是空列表 = 真的没有越界）
    - `missing`：**键根本不存在**——这是 `illegal` 字段上线**之前**的旧记录。
      它与 `illegal: []` 含义完全相反：前者是「没测」，后者是「测了，没越界」。
      混为一谈就会让旧记录以干净身份进分母——这正是本模块 docstring 骂
      `eval_soft_prefs.py` 的「构造性的 0」。
    - `shape`：键在，但类型不对（不是 list / 元素不是 dict）。同样不可判定，
      **不能当成「干净」**。

    subagent 审查实测：把这三种都当空列表时，25 条记录会得到
    `越界率 0.0 + sample_ok=True + 判定「通过」`——正是本工具要消灭的假绿。
    """
    if "illegal" not in row:
        return None, "missing"
    value = row["illegal"]
    if not isinstance(value, list):
        return None, "shape"
    if not all(isinstance(item, dict) for item in value):
        return None, "shape"
    return list(value), ""


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def aggregate(lines: Iterable[Any]) -> dict:
    """shadow 单行 JSON 记录 → 抽取质量 / 封闭失效聚合。

    口径全部写在模块 docstring 里；这里只做计数。**每一条「剔出分母」的路径
    都要单列计数并参与判定**——只要有一类被静默当成「干净」，报告就会在
    什么都没测到的情况下给出「通过」。
    """
    _skipped[0] = 0
    rows = _parse_rows(lines)
    skipped = _skipped[0]
    total = len(rows)

    status_counter: Counter = Counter()
    version_counter: Counter = Counter()
    reason_counter: Counter = Counter()
    field_counter: Counter = Counter()
    applied = 0

    responded = 0        # LLM 真的响应过 **且** 字段形状完好（越界率的分母）
    responded_with_illegal = 0
    all_rejected = 0
    unknown = 0
    no_illegal_field = 0   # 旧记录：illegal 键不存在
    illegal_shape_errors = 0  # illegal 存在但形状不对
    seen: set[str] = set()   # 已计入分母的 message_head（**去重后**才算样本量）
    duplicates = 0
    dirty_samples: list[dict] = []

    for row in rows:
        version_counter[str(row.get("version", "?"))] += 1
        if row.get("mode_applied") is True:
            applied += 1

        head = str(row.get("message_head", ""))
        is_repeat = head in seen
        if is_repeat:
            duplicates += 1

        # 脏 status：不认得的值按 unknown 处理，绝不当成「干净」
        raw_status = row.get("status")
        status = raw_status if raw_status in STATUSES else "unknown"
        status_counter[status] += 1
        if status == "unknown":
            unknown += 1

        entries, problem = _illegal_entries(row)
        if problem == "missing":
            no_illegal_field += 1
        elif problem == "shape":
            illegal_shape_errors += 1

        if problem == "" and entries:
            dirty_samples.append({
                "status": status,
                "message_head": head[:60],
                "illegal": entries,
            })
            for item in entries:
                reason_counter[str(item.get("reason", "?"))] += 1
                field_counter[str(item.get("field", "?"))] += 1

        # 分母只认「响应过 **且** 观测字段完好」的记录
        if status not in _RESPONDED or problem != "":
            continue
        # 同一句话被重复落盘时**只计一次**：10 句 × 3 副本凑成「30 条样本」
        # 是在灌水，样本量必须按**去重后的独立消息**算（审查实测：会判「通过」）。
        if is_repeat:
            continue
        seen.add(head)
        responded += 1
        if entries:
            responded_with_illegal += 1
        if status == "all_rejected":
            all_rejected += 1

    illegal_rate = _rate(responded_with_illegal, responded)
    empty_rate = _rate(all_rejected, responded)

    # ── 判定：按「先致命、后数值」的顺序，任何一条不满足都不得判通过 ──
    blockers: list[str] = []
    if no_illegal_field:
        blockers.append(
            f"{no_illegal_field} 条记录**没有 illegal 字段**（{_ILLEGAL_FIELD_HINT}）——"
            "那是「没测」，不是「没越界」"
        )
    if illegal_shape_errors:
        blockers.append(f"{illegal_shape_errors} 条记录的 illegal 形状不对，不可判定")
    if unknown:
        blockers.append(f"{unknown} 条 status=unknown（未埋点或重复埋点）")
    if applied:
        blockers.append(
            f"{applied} 条记录 mode_applied=true——shadow 模式下必须恒为 0，"
            "模式可能被搞错了"
        )
    if empty_rate is not None and empty_rate > _MAX_EMPTY_RATE:
        blockers.append(
            f"空偏好率 {_pct(empty_rate)} 超过 {_MAX_EMPTY_RATE:.0%}"
            "——模型每条都在说废话却从不越界，说明它没在按 schema 思考，"
            "「越界率 0」没有意义"
        )
    if responded < _MIN_SAMPLE:
        blockers.append(f"样本不足：响应样本 {responded} 条 < 下限 {_MIN_SAMPLE} 条")
    if illegal_rate is None:
        blockers.append("没有任何 LLM 响应过且字段完好的记录")
    elif illegal_rate > 0:
        blockers.append(f"越界率 {_pct(illegal_rate)} 非 0")

    # 判定前缀按**性质**分：越界/模式错 = 不通过；其余（没测到、样本不够）
    # = 不可判定。把「判不了」说成「不通过」会误导——它意味着再攒数据，
    # 而不是这条路走不通。退出码与之一致。
    failed = bool(illegal_rate) or bool(applied)
    if blockers:
        verdict = ("不通过：" if failed else "不可判定：") + "；".join(blockers)
        exit_code = 1 if failed else 2
    else:
        verdict = "通过：越界率 0"
        exit_code = 0

    return {
        "total": total,
        "skipped": skipped,
        "status_distribution": dict(status_counter.most_common()),
        "version_distribution": dict(version_counter.most_common()),
        "responded": responded,
        "responded_with_illegal": responded_with_illegal,
        "illegal_rate": illegal_rate,
        "all_rejected": all_rejected,
        "empty_preference_rate": empty_rate,
        "no_response": status_counter.get("no_response", 0),
        "unknown": unknown,
        "no_illegal_field": no_illegal_field,
        "illegal_shape_errors": illegal_shape_errors,
        "duplicates": duplicates,
        "applied": applied,
        "sample_ok": not blockers,
        "min_sample": _MIN_SAMPLE,
        "reason_distribution": dict(reason_counter.most_common()),
        "field_distribution": dict(field_counter.most_common()),
        "dirty_samples": dirty_samples,
        "verdict": verdict,
        "exit_code": exit_code,
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def render_markdown(report: dict) -> str:
    """聚合结果 → markdown 报告。"""
    dist = report.get("status_distribution") or {}
    lines = [
        "# Soft 偏好 shadow 报告",
        "",
        f"**判定：{report.get('verdict')}**",
        "",
        f"- 记录总数：{report.get('total', 0)}"
        f"（LLM 响应过 **且** 观测字段完好 {report.get('responded', 0)} 条 = 越界率的分母；"
        f"未响应 {report.get('no_response', 0)} 条 = 超时/异常/空内容/客户端不可用，"
        f"**不进分母**——否则大面积超时会伪装成越界率 0）",
        f"- **越界率：{_pct(report.get('illegal_rate'))}**"
        f"（含封闭失效 {report.get('responded_with_illegal', 0)} / 响应 {report.get('responded', 0)}）",
        f"- 空偏好率（响应中一条字段都没通过）：{_pct(report.get('empty_preference_rate'))}",
        f"- 落画像记录数：{report.get('applied', 0)}"
        "（shadow 模式下**必须恒为 0**；非 0 说明模式被搞错了）",
        f"- 被丢弃的脏行：{report.get('skipped', 0)}"
        f"（重复记录 {report.get('duplicates', 0)} 条——同一句被重复落盘会灌大分母）",
        "",
    ]

    invisible = report.get("no_illegal_field", 0) + report.get("illegal_shape_errors", 0)
    if invisible:
        lines += [
            f"## ⛔ 观测字段不可用：{invisible} 条",
            "",
            f"- 缺 `illegal` 键：{report.get('no_illegal_field', 0)} 条"
            f"（{_ILLEGAL_FIELD_HINT}）",
            f"- `illegal` 形状不对：{report.get('illegal_shape_errors', 0)} 条",
            "",
            "这些记录是「**没测**」，不是「测了没越界」，一律已剔出分母。",
            "把它们当干净样本，正是本工具存在的理由所要防止的事。",
            "",
        ]

    if report.get("unknown"):
        lines += [
            f"## ⛔ status=unknown：{report['unknown']} 条",
            "",
            "「unknown」表示这条路径**没有埋点或埋了不止一次**——既可能是没问题，"
            "也可能是没测到，**不能默认按干净算**。先查埋点，再谈越界率。",
            "",
        ]

    if not report.get("sample_ok"):
        lines += [
            f"> ⚠️ **样本强度不足**：响应样本 {report.get('responded', 0)} 条，"
            f"下限 {report.get('min_sample', _MIN_SAMPLE)} 条；"
            f"`unknown` {report.get('unknown', 0)} 条。"
            "此越界率仅供参考，不足以支撑切流判据。",
            "",
        ]

    lines += [
        "## 状态分布",
        "",
        "| status | 条数 | 说明 |",
        "|---|---|---|",
    ]
    meaning = {
        "ok": "至少一个字段通过校验",
        "all_rejected": "响应了，但输出没通过任何字段校验",
        "no_response": "没调通（超时/异常/空内容/客户端不可用）",
        "unknown": "未埋点或重复埋点——不可判定",
    }
    for key in list(STATUSES) + [k for k in dist if k not in STATUSES]:
        if key not in dist:
            continue
        lines.append(f"| `{key}` | {dist[key]} | {meaning.get(key, '（未知取值）')} |")

    lines += [
        "",
        "## 封闭失效构成",
        "",
        "| reason | 条数 |",
        "|---|---|",
    ]
    reasons = report.get("reason_distribution") or {}
    if reasons:
        for name, count in reasons.items():
            lines.append(f"| `{name}` | {count} |")
    else:
        lines.append("| （无） | 0 |")

    lines += [
        "",
        "## 按字段分布",
        "",
        "| field | 条数 |",
        "|---|---|",
    ]
    fields = report.get("field_distribution") or {}
    if fields:
        for name, count in fields.items():
            escaped = str(name).replace("|", "\\|")
            lines.append(f"| `{escaped}` | {count} |")
    else:
        lines.append("| （无） | 0 |")

    lines += [
        "",
        f"## 越界样本（{len(report.get('dirty_samples') or [])} 条）",
        "",
        "| # | status | message_head | reason | field | value |",
        "|---|---|---|---|---|---|",
    ]
    samples = report.get("dirty_samples") or []
    if samples:
        for i, sample in enumerate(samples[:50], start=1):
            head = str(sample.get("message_head", "")).replace("|", "\\|")
            for j, item in enumerate(sample.get("illegal") or [], start=1):
                # 反斜杠转义必须提到 f-string 外面：f-string 内允许转义是 PEP 701，
                # Python 3.12 才合法；本仓对外声明支持 3.11+（同 shadow_report）。
                value = str(item.get("value", "")).replace("|", "\\|")
                field = str(item.get("field", "")).replace("|", "\\|")
                index = f"{i}" if j == 1 else f"{i}.{j}"
                lines.append(
                    f"| {index} | {sample.get('status')} | {head} "
                    f"| {item.get('reason')} | {field} | {value} |"
                )
    else:
        lines.append("| （无） | - | - | - | - | - |")
    if len(samples) > 50:
        lines.append("")
        lines.append(f"> 仅列前 50 条，共 {len(samples)} 条。")

    lines += [
        "",
        "## 跨版本",
        "",
        "| version | 条数 |",
        "|---|---|",
    ]
    versions = report.get("version_distribution") or {}
    if versions:
        for name, count in versions.items():
            lines.append(f"| `{name}` | {count} |")
    else:
        lines.append("| （无） | 0 |")
    lines.append("")
    lines.append(
        "> 换提示词或换词表来源时 `SOFT_PREF_VERSION` 必须递增——"
        "新旧数据混算出来的越界率没有意义。"
    )
    lines.append("")
    return "\n".join(lines)


def _st_row(**kw: Any) -> str:
    """自验证专用样本行——必须带 `logger` 字段。

    少了它就会被 `_is_shadow_row` 挡掉（这正是该守门规则存在的意义：
    初版自验证的样本没带 `logger`，自己把自己的规则测挂了）。

    `message_head` 默认唯一：分母按去重后的独立消息算，共用 head 会被判成重复。
    """
    base = {
        "logger": SHADOW_LOGGER,
        "version": "soft-pref-v1",
        "mode_applied": False,
        "status": "ok",
        "illegal": [],
        "prefs": {"pain_points": ["续航"]},
        "message_head": f"我平时通勤-{next(_st_seq)}",
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def _self_test() -> int:
    """自验证：构造几组**已知答案**的记录，断言聚合口径正确。

    没有自验证的统计脚本，数字错了也没人知道——尤其是分母这种最容易搞错的地方。
    """
    cases: list[tuple[str, list[str], dict]] = [
        (
            "分母只算响应过的：大量超时不得压低越界率",
            [
                _st_row(status="no_response", prefs=None),
                _st_row(status="no_response", prefs=None),
                _st_row(),
            ],
            {"responded": 1, "illegal_rate": 0.0, "no_response": 2},
        ),
        (
            "一条越界 → 越界率 0.5（不是 1/3）",
            [
                _st_row(illegal=[{"field": "usage_scenario",
                                 "reason": "out_of_enum", "value": "商务舱"}]),
                _st_row(),
            ],
            {"responded": 2, "illegal_rate": 0.5, "no_response": 0},
        ),
        (
            "all_rejected 进分母：模型响应过就算数",
            [_st_row(status="all_rejected", prefs=None)],
            {"responded": 1, "empty_preference_rate": 1.0},
        ),
        (
            "unknown 不进分母，且判不可判定",
            [_st_row(status="unknown", prefs=None), _st_row()],
            {"responded": 1, "unknown": 1},
        ),
        (
            "脏 status 按 unknown 处理，不当成 ok",
            [_st_row(status="no-reponse", prefs=None)],
            {"responded": 0, "unknown": 1},
        ),
        (
            "非 shadow 记录（别的 logger）不得混入分母",
            [json.dumps({"logger": "app.agent.router.shadow", "utterance": "20万预算"})],
            {"total": 0, "responded": 0},
        ),
    ]
    failed = 0
    for label, rows, expect in cases:
        got = aggregate(rows)
        for key, want in expect.items():
            if got.get(key) != want:
                failed += 1
                print(f"[FAIL] {label}: {key} 期望 {want}，实际 {got.get(key)}")
        print(f"[{'OK  ' if not failed else '..  '}] {label}")
    print(f"\n自验证：{'全部通过' if failed == 0 else f'{failed} 项不符'}")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="soft 偏好 shadow 报告（app.agent.soft_prefs.shadow 日志 → markdown）"
    )
    parser.add_argument("--file", help="shadow 单行 JSON 日志文件路径（缺省读 stdin）")
    parser.add_argument("--out", help="markdown 输出文件路径（缺省写 stdout）")
    parser.add_argument("--self-test", action="store_true", help="只跑口径自验证并退出")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.file:
        lines: list[str] = Path(args.file).read_text(
            encoding="utf-8", errors="replace"  # 日志可能混入非 UTF-8 字节，容错不崩
        ).splitlines()
    else:
        # Windows 控制台管道默认非 UTF-8：显式重配，避免中文日志读成乱码
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        lines = sys.stdin.read().splitlines()

    report = aggregate(lines)
    markdown = render_markdown(report)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8", newline="\n")
    else:
        # stdout **必须**同样重配：报告里有 ⚠️ / ⛔，Windows 控制台默认 cp936
        # 编不了这两个字符，会在「样本不足 / unknown」这两条警告路径上直接崩——
        # 也就是本工具唯一的存在理由。（审查实测：未重配时 UnicodeEncodeError。）
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.write(markdown)
    # 退出码承载判定：0=通过 / 1=明确不通过（越界或模式错）/ 2=不可判定或样本不足。
    # 「不可判定」单独一档，是为了让 CI 不把「没测到」读成「通过」。
    return int(report.get("exit_code", 2))


if __name__ == "__main__":
    raise SystemExit(main())
