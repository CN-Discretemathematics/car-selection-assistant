r"""可追问率评测（计划 §5.1「新增的可度量指标」）。

定义（计划原文）：随机抽 N 条已回答的问题，把答案给用户看并问「这句话的依据是什么」，
系统能自动给出该句数字出处的比例。

为什么必须先有它：目前评测量的是「检索准不准」「答得对不对」，
**没有量「用户能否问出这句话的出处」**——而这是唯一的差异化承诺（ADR-0003）。
没有度量，差异化只是口号。

## 口径（本工具的实现，全部写死在这里，便于审计）

- **被测文本**：本次回答的**用户可见正文**（`explanation` + `reasons`）。
- **依据集合**（= 系统回答这个问题时手上有的东西）：
  1. **卡片**：本轮返回的候选（价格、评分、展示名）；
  2. **库内事实**：回答涉及的**车系**的官方指导价与参数事实（`OfficialPrice` /
     `SpecFact`，每条都带 `source_id`）——这就是「系统能自动给出出处」的真凭据；
  3. **来源引用**：本轮 `citations`；
  4. **用户自述**：画像里的数值 + 本轮消息里的数字。
  ⚠️ 依据集合**不含被测正文本身**：否则「正文里的数字在正文里」恒真，指标当场作废
  （第一版探针就栽在这里）。
- **可追问** = 该数字能在依据集合里找到对应项。数值比较沿用回答契约的既有口径：
  数值集合 + 0.01 容差 + **万元派生**（工具返回「元」、文案说「万元」是同一事实的
  两种写法，不是编造）。
- **四个桶**：
  | 桶 | 含义 |
  | --- | --- |
  | `source` | 落在卡片/库内事实/来源引用上——有出处 |
  | `user` | 用户自己说过的（预算、座位数…） |
  | `derived` | 由查询条件数出来的**计数**（款/个/家…）——出处是查询条件本身 |
  | `none` | **没有出处**＝不可追问（潜在的编造或口径漂移） |

### 刻意收窄/放宽的三处（与既有口径的差别，写明以免后人当漂移）

1. **万元派生只对 ≥10000 的值生效**。回答契约（`answer_contract.tool_result_universe`）
   对**所有**数字都派生 `/10000`（它是硬闸门，宁可误拦）；本工具是**度量**，
   误报会把率抬高——50000 元派生出的「5」会让「共 5 款」被误判成「有出处」。
   汽车的指导价不可能低于 1 万，所以按 ≥10000 收窄不丢真实场景。
2. **千分位合并**。`199,800` 是**一个**数；按 `\d+` 切会切成 199 与 800 两个，
   于是「有出处」被判成「没出处」（实测：模型爱写千分位，8 题里 1 题全错）。
3. **计数类数字不查库**。「共 12 款」这类数字的出处是**查询条件**（哪套过滤），
   复现条件就得复制各路径的筛选逻辑（漂移风险）。本工具只量「能不能问出出处」，
   不量「数值对不对」（正确性由既有的取舍/硬约束评测负责），故计数一律记 `derived`。

用法（backend/ 下）：
    python tools/eval_traceability.py --questions eval/questions.json --limit 30
退出码：0 = 跑完（报告里给出率）；2 = 题库缺失（本地专属文件，CI 检不出）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path)
from sqlalchemy import select  # noqa: E402

from app.agent.engine import AgentEngine  # noqa: E402
from app.agent.session import SessionStore  # noqa: E402
from app.catalog.series_index import resolve_series  # noqa: E402
from app.common.database import get_session_factory  # noqa: E402
from app.common.llm import LLMClient  # noqa: E402
from app.common.models import (  # noqa: E402
    MonthlySales,
    OfficialPrice,
    SpecFact,
    VehicleSeries,
    VehicleVariant,
)

KIND_SOURCE = "source"
KIND_USER = "user"
KIND_DERIVED = "derived"
KIND_NONE = "none"

#: 优先级：同一数字有多处出处时，报**最具体**的那一处。
_KIND_PRIORITY = {KIND_SOURCE: 0, KIND_USER: 1, KIND_DERIVED: 2}

#: 万元派生的门槛（元）。低于它的值不派生——见 docstring「刻意收窄的第 1 处」。
_YUAN_THRESHOLD = 10000

#: 千分位：199,800 是一个数，不是两个。
_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

#: 句子的切分符（中英文标点 + 换行）。
_SENTENCE_SPLIT = re.compile(r"[。！？；!?;\n]+")

#: 计数词：数字后面跟着这些词，就是「数出来的东西」而不是某个事实的值。
_COUNT_UNITS = ("款", "个", "家", "辆", "台", "名", "项", "条", "种")
_COUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:" + "|".join(_COUNT_UNITS) + r")")

#: 单位刻度：`L/100km`、`0-100km/h` —— 这里的数字是**单位的一部分**，不是数据值。
#: 不排除掉就会把「油耗 6.35 L/100km」里的 100 判成「没有出处」（实测：30 题里 14 题命中）。
_UNIT_SCALE_RE = re.compile(r"(?<=/)\s*100(?=\s*km)|(?<![0-9.])0\s*[-–—~至]\s*100(?=\s*km/h)")

#: 示例：`比如「预算15万，家用5口人」` —— 里面的数字是**假设**，不是事实主张。
#: 实测：品牌盘点这类问题在信息不足时，正文是一句反问 + 一个示例；
#: 那些示例数字曾被算成「没有出处」（追问轮没走 need_clarification 标志，判不出来）。
_EXAMPLE_RE = re.compile(r"(?:比如|例如|举例)\s*[：:]?\s*「[^」]*」")


def numbers_in(text: str) -> list[float]:
    """文本里的数值，**保留千分位**（`199,800` 是一个数），**排除单位刻度与示例**。

    与 `answer_contract.numbers_in` 的差别有三处：千分位不被拆开；
    单位刻度被排除；示例里的数字被排除。
    """
    text = text or ""
    skip = [m.span() for m in _UNIT_SCALE_RE.finditer(text)]
    skip += [m.span() for m in _EXAMPLE_RE.finditer(text)]
    out: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in skip):
            continue
        token = match.group(0)
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:  # 理论上到不了（正则已保证是数字）
            continue
    return out


@dataclass
class Basis:
    """依据集合：数字 → 出处（值 → 若干条出处说明）。

    同一个数字可能有多处出处（如「20 万」既是某车价格、也是用户说的预算）。
    报告取**最具体**的那条，但明细里保留全部。
    """

    origins: dict[float, list[tuple[str, str]]] = field(default_factory=dict)

    def add(self, value: float, kind: str, label: str) -> None:
        """登记一个数字的出处；万元口径同时登记派生值。"""
        self._add_raw(value, kind, label)
        if abs(value) >= _YUAN_THRESHOLD:
            self._add_raw(round(value / 10000, 3), kind, f"{label}（万元口径）")

    def _add_raw(self, value: float, kind: str, label: str) -> None:
        key = round(float(value), 3)
        bucket = self.origins.setdefault(key, [])
        if (kind, label) not in bucket:
            bucket.append((kind, label))

    def _hits(self, value: float) -> list[tuple[str, str]]:
        target = float(value)
        hits: list[tuple[str, str]] = []
        for key, entries in self.origins.items():
            if abs(key - target) < 0.01 or (target != 0 and abs(key * 10000 - target) < 0.01):
                hits.extend(entries)
        return hits

    def lookup(self, value: float) -> tuple[str, str] | None:
        """找一个数字的出处（取优先级最高的一条）；没有则 None。"""
        hits = self._hits(value)
        if not hits:
            return None
        return sorted(hits, key=lambda e: _KIND_PRIORITY.get(e[0], 9))[0]


def _walk(node, out: list[tuple[float, str]]) -> None:
    """把任意 JSON 结构里的数值连同它的键名摊平。

    ⚠️ **列表里的标量也要收**（`locked_series_ids: [905]` → 905）。
    第一版只处理 dict 的值、递归时把列表元素整个丢掉，于是画像里的
    `locked_series_ids` 数字没进依据集合 → 「车系 ID 905」被判成「没有出处」
    （口径自己的 bug，被实测逮到）。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                out.append((float(value), str(key)))
            elif isinstance(value, str):
                for n in numbers_in(value):
                    out.append((n, str(key)))
            else:
                _walk(value, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            if isinstance(item, bool) or item is None:
                continue
            if isinstance(item, (int, float)):
                out.append((float(item), ""))
            elif isinstance(item, str):
                for n in numbers_in(item):
                    out.append((n, ""))
            else:
                _walk(item, out)


def _series_numbers_from_db(db, series_ids: set[int]) -> list[tuple[float, str]]:
    """库内事实里的数值 → (值, 出处说明)。出处说明带 source_id。

    这是「系统能自动给出出处」的真凭据：价格与参数事实都挂着来源。
    """
    out: list[tuple[float, str]] = []
    if not series_ids:
        return out
    ids = sorted(series_ids)
    price_rows = db.execute(
        select(VehicleVariant.display_name, OfficialPrice.price_cny, OfficialPrice.source_id)
        .join(OfficialPrice, OfficialPrice.variant_id == VehicleVariant.id)
        .where(VehicleVariant.series_id.in_(ids))
    ).all()
    for name, price, source_id in price_rows:
        if price is None:
            continue
        out.append((float(price), f"{name}·官方指导价（来源{source_id}）"))
    fact_rows = db.execute(
        select(
            VehicleVariant.display_name,
            SpecFact.fact_key,
            SpecFact.fact_value,
            SpecFact.source_id,
        )
        .join(SpecFact, SpecFact.variant_id == VehicleVariant.id)
        .where(VehicleVariant.series_id.in_(ids))
    ).all()
    for name, key, value, source_id in fact_rows:
        for n in numbers_in(value or ""):
            out.append((n, f"{name}·{key}（来源{source_id}）"))
        # 事实**键名**里的数字也是记录的一部分（实测：「行李厢12V电源接口 = 有」
        # 里的 12 来自键名，不是数据值——不登记就会被判成「没有出处」）。
        for n in numbers_in(key or ""):
            out.append((n, f"{name}·{key}（来源{source_id}）"))
    # 销量（实测漏掉过：「月销量 25,158 辆」里的 25158 是真实库内数据，带 source_id）。
    sales_rows = db.execute(
        select(
            VehicleSeries.name,
            MonthlySales.month,
            MonthlySales.sales_count,
            MonthlySales.source_id,
        )
        .join(VehicleSeries, VehicleSeries.id == MonthlySales.series_id)
        .where(VehicleSeries.id.in_(ids))
    ).all()
    for name, month, count, source_id in sales_rows:
        if count is None:
            continue
        out.append((float(count), f"{name}·{month} 月销量（来源{source_id}）"))
    # 车系名 / 款型名里的数字（实测漏掉过：「问界M7」的 7、「IONIQ 5 N」的 5）。
    # 名字本身来自库 → 这些数字**有出处**（出处就是名字）。
    name_rows = db.execute(
        select(VehicleSeries.name, VehicleVariant.display_name)
        .join(VehicleVariant, VehicleVariant.series_id == VehicleSeries.id)
        .where(VehicleSeries.id.in_(ids))
    ).all()
    for series_name, display_name in name_rows:
        for n in numbers_in(series_name or ""):
            out.append((n, f"{series_name}·车系名"))
        for n in numbers_in(display_name or ""):
            out.append((n, f"{display_name}·款型名"))
    return out


def build_basis(
    *,
    db=None,
    variants: list[dict] | None = None,
    citations: list[dict] | None = None,
    profile: dict | None = None,
    user_text: str = "",
    answer_text: str = "",
) -> Basis:
    """构造依据集合（**不含被测正文**——见 docstring）。"""
    basis = Basis()
    variants = variants or []
    series_ids: set[int] = set()
    for variant in variants:
        label = str(variant.get("display_name") or variant.get("series_name") or "候选")
        if variant.get("series_id") is not None:
            series_ids.add(int(variant["series_id"]))
        flat: list[tuple[float, str]] = []
        _walk(variant, flat)
        for value, key in flat:
            basis.add(value, KIND_SOURCE, f"{label}·{key}")
    for citation in citations or []:
        flat = []
        _walk(citation, flat)
        for value, key in flat:
            basis.add(value, KIND_SOURCE, f"来源引用·{key}")

    # 回答里提到的车系（问答路径没有卡片，只能从正文解析车系名）
    if db is not None:
        probe = answer_text or user_text
        try:
            for series, _brand in resolve_series(db, probe):
                series_ids.add(int(series.id))
        except Exception:  # noqa: BLE001 —— 解析失败不该让评测工具整个挂掉
            pass
        for value, label in _series_numbers_from_db(db, series_ids):
            basis.add(value, KIND_SOURCE, label)

    flat = []
    _walk(profile, flat)
    for value, key in flat:
        basis.add(value, KIND_USER, f"你说的·{key}")
    for value in numbers_in(user_text):
        basis.add(value, KIND_USER, "你原话里的数字")

    basis.add(float(len(variants)), KIND_DERIVED, "本轮展示的候选数")
    basis.add(float(len(citations or [])), KIND_DERIVED, "本轮引用的来源数")
    return basis


def split_sentences(text: str) -> list[str]:
    """按中文/英文句读切句。"""
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text or "")]
    return [p for p in parts if p]


def _count_values(text: str) -> set[float]:
    """文本里的**计数类**数字（后面跟着「款/个/家」这类数量词）。"""
    return {float(m.group(1)) for m in _COUNT_RE.finditer(text or "")}


def trace_text(text: str, basis: Basis) -> list[dict]:
    """逐个数字判定出处。"""
    counts = _count_values(text)
    rows: list[dict] = []
    for value in numbers_in(text):
        origin = basis.lookup(value)
        if origin is not None:
            kind, label = origin
        elif value in counts:
            kind, label = KIND_DERIVED, "计数（由查询条件数出）"
        else:
            kind, label = KIND_NONE, ""
        rows.append({"value": value, "kind": kind, "label": label})
    return rows


def report(answer_text: str, basis: Basis) -> dict:
    """一条回答的可追问率报告（数字级 + 句级）。"""
    rows = trace_text(answer_text, basis)
    by_kind = {KIND_SOURCE: 0, KIND_USER: 0, KIND_DERIVED: 0, KIND_NONE: 0}
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1

    sentences = split_sentences(answer_text)
    scored = 0
    traceable = 0
    bad_sentences: list[str] = []
    for sentence in sentences:
        srows = trace_text(sentence, basis)
        if not srows:
            continue  # 不含数字的句子不提出可追问主张，不进分母
        scored += 1
        if all(r["kind"] != KIND_NONE for r in srows):
            traceable += 1
        else:
            bad_sentences.append(sentence)

    total = len(rows)
    answered = total - by_kind[KIND_NONE]
    return {
        "numbers": total,
        "numbers_traceable": answered,
        "number_rate": round(answered / total, 4) if total else None,
        "by_kind": by_kind,
        "sentences_scored": scored,
        "sentences_traceable": traceable,
        "sentence_rate": round(traceable / scored, 4) if scored else None,
        "untraceable": [r for r in rows if r["kind"] == KIND_NONE],
        "untraceable_sentences": bad_sentences,
    }


async def _run_one(db, engine: AgentEngine, text: str):
    store = engine._store  # noqa: SLF001 —— 评测工具与既有 eval_agent.py 同款取法
    sid = store.create()
    out = await engine.handle(db, sid, text)
    profile = store.get_profile(sid) or {}
    return out, profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="可追问率评测（计划 §5.1）")
    parser.add_argument("--questions", default=os.path.join("eval", "questions.json"))
    parser.add_argument("--limit", type=int, default=0, help="只评测前 N 条（0=全部）")
    parser.add_argument("--report", default=os.path.join("eval", "traceability_report.json"))
    parser.add_argument("--use-llm", action="store_true", help="用真实模型出解释（默认走确定性模板）")
    args = parser.parse_args(argv)

    path = Path(args.questions)
    if not path.exists():
        print(f"题库不存在：{path}\n"
              f"题库是**本地专属**文件（不入仓库），先跑 tools/gen_eval_questions.py 生成。")
        return 2
    questions = json.loads(path.read_text(encoding="utf-8"))["questions"]
    if args.limit:
        questions = questions[: args.limit]

    llm = LLMClient(api_key=None) if not args.use_llm else None
    engine = AgentEngine(llm=llm, store=SessionStore())

    rows: list[dict] = []
    agg = {"numbers": 0, "numbers_traceable": 0, "sentences_scored": 0, "sentences_traceable": 0}
    by_kind: dict[str, int] = {KIND_SOURCE: 0, KIND_USER: 0, KIND_DERIVED: 0, KIND_NONE: 0}

    with get_session_factory()() as db:
        for q in questions:
            try:
                out, profile = asyncio.run(_run_one(db, engine, q["text"]))
            except Exception as err:  # noqa: BLE001
                rows.append({"id": q.get("id"), "error": f"{type(err).__name__}: {err}"})
                continue
            if out.need_clarification:
                # 追问轮**不提出事实主张**——正文里那句「比如「预算15万，家用5口人」」
                # 是给用户的**示例**，把它算进分母等于拿示例数字当编造。
                rows.append({"id": q.get("id"), "intent": q.get("intent"), "skipped": "clarify"})
                continue
            answer = "\n".join([out.explanation or "", *out.reasons])
            basis = build_basis(
                db=db,
                variants=[v.model_dump() for v in out.recommended_variants],
                citations=[c.model_dump() for c in out.citations],
                profile=profile,
                user_text=q["text"],
                answer_text=answer,
            )
            rep = report(answer, basis)
            agg["numbers"] += rep["numbers"]
            agg["numbers_traceable"] += rep["numbers_traceable"]
            agg["sentences_scored"] += rep["sentences_scored"]
            agg["sentences_traceable"] += rep["sentences_traceable"]
            for kind, count in rep["by_kind"].items():
                by_kind[kind] = by_kind.get(kind, 0) + count
            rows.append({
                "id": q.get("id"),
                "intent": q.get("intent"),
                "text": q["text"][:60],
                "numbers": rep["numbers"],
                "number_rate": rep["number_rate"],
                "sentence_rate": rep["sentence_rate"],
                "by_kind": rep["by_kind"],
                "untraceable": rep["untraceable"][:6],
            })

    number_rate = agg["numbers_traceable"] / agg["numbers"] if agg["numbers"] else None
    sentence_rate = (
        agg["sentences_traceable"] / agg["sentences_scored"] if agg["sentences_scored"] else None
    )
    out_report = {
        "questions_file": str(path),
        "total": len(rows),
        "numbers": agg["numbers"],
        "numbers_traceable": agg["numbers_traceable"],
        "number_rate": round(number_rate, 4) if number_rate is not None else None,
        "sentences_scored": agg["sentences_scored"],
        "sentences_traceable": agg["sentences_traceable"],
        "sentence_rate": round(sentence_rate, 4) if sentence_rate is not None else None,
        "by_kind": by_kind,
        "rows": rows,
    }
    out_path = Path(args.report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"样本 {len(rows)} 条")
    if number_rate is not None:
        print(f"  数字 {agg['numbers']} 个，可追问 {agg['numbers_traceable']} 个（数字级 {number_rate:.1%}）")
    if sentence_rate is not None:
        print(f"  含数字的句子 {agg['sentences_scored']} 句，可追问 {agg['sentences_traceable']} 句"
              f"（句级 {sentence_rate:.1%}）")
    print(f"  分桶：{by_kind}")
    print(f"报告已写入：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
