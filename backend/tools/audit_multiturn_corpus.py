"""多轮题库的**变量分布审计**（计划 §七：「50 题/场景足以判定」——需先看分布是否真的覆盖）。

计划 §3.3 的原话：

> **必须保证每场景 50 题在关键变量上有分布**（品牌数、价位段、拒绝维度各覆盖多少），
> 否则 50 题的统计意义≈5 题。

本工具就是把那三句话变成可执行的检查：

1. **每个场景的题数、轮次数、`enabled` 状态**（`enabled=False` = 该场景依赖尚未
   上线的能力，如跨会话记忆与「忘掉」——**今天跑不了**，不能算进有效样本）；
2. **关键变量的取值数与每取值题数**：价位段 / 品牌 / 车身 / 能源 / 人数 / 拒绝理由；
3. **最小分层单元格**（价位段 × 拒绝理由）：某一格只有 1~2 题时，
   「按该格统计」就等于没统计；
4. **断言的误伤面**（需要 DB）：「忘掉预算后不许再提 12 万」这类断言若用裸字符串
   匹配，而答案里合法地出现了「12.18 万」，断言会把**正确答案判成失败**
   （假否定 ⇒ 低估能力）。

用法（backend/ 下）：
    python tools/audit_multiturn_corpus.py --corpus eval/multiturn.json
    python tools/audit_multiturn_corpus.py --corpus eval/multiturn.json --check-asserts
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path)

# 「预算18到25万，埃安的轿车，燃油的，家里3口人用」
_BUDGET_RE = re.compile(r"预算(\d+)到(\d+)万")
_BRAND_RE = re.compile(r"([一-龥A-Za-z0-9]+)的")
_BODY_RE = re.compile(r"的(轿车|SUV|MPV|皮卡)")
_ENERGY_RE = re.compile(r"(纯电|插混|增程|燃油|油混|混动)")
_PEOPLE_RE = re.compile(r"家里(\d+)口人")

#: 拒绝理由的分类（实测题库里的 42 种拒绝句归纳出的 6 类 + 兜底）。
#: ⚠️ 顺序有讲究：「太贵了」与「预算超了」都是价格；「太大了」是尺寸、不是空间
#: （「空间不够」才是空间）；「不喜欢这个外观」是外观、不是配置。
_REASON_RULES: tuple[tuple[str, str], ...] = (
    ("品牌", r"(这个品牌|品牌我不想要)"),
    ("价格", r"(太贵|贵了|预算超)"),
    ("外观", r"(外观|样子)"),
    ("能耗", r"(省油|更省油|油耗)"),
    ("空间", r"(空间不够|空间小|后排不够)"),
    ("尺寸", r"(太大了|太大|尺寸)"),
    ("配置", r"(配置|内饰|颜色)"),
)

#: 断言里的「关键词」——`mentions` / `not_mentions` 的 word 字段。
_WORD_RE = re.compile(r"'(?P<word>[^']+)'")


def _parse_turn1(text: str) -> dict:
    """解析第一轮的需求句（模板生成，结构固定）。"""
    out: dict = {}
    m = _BUDGET_RE.search(text)
    if m:
        out["price_band"] = f"{m.group(1)}-{m.group(2)}万"
    m = _BRAND_RE.search(text)
    if m:
        out["brand"] = m.group(1)
    m = _BODY_RE.search(text)
    if m:
        out["body"] = m.group(1)
    m = _ENERGY_RE.search(text)
    if m:
        out["energy"] = m.group(1)
    m = _PEOPLE_RE.search(text)
    if m:
        out["people"] = f"{m.group(1)}口"
    return out


def _parse_reason(text: str) -> str:
    """解析拒绝理由（第二轮）：价格 / 品牌 / 外观 / 能耗 / 空间 / 尺寸 / 配置 / 未归类。"""
    for label, pattern in _REASON_RULES:
        if re.search(pattern, text):
            return label
    return "未归类"


def audit(corpus: dict) -> dict:
    """按场景汇总分布。"""
    scenarios: dict[str, list[dict]] = defaultdict(list)
    for item in corpus.get("scenarios", []):
        scenarios[item.get("scenario", "?")].append(item)

    report: dict = {"scenarios": {}}
    for name, items in scenarios.items():
        first_turns = [i["turns"][0]["user"] for i in items if i.get("turns")]
        parsed = [_parse_turn1(t) for t in first_turns]
        reasons = [
            _parse_reason(i["turns"][1]["user"])
            for i in items
            if len(i.get("turns", [])) > 1
        ]
        cells: Counter = Counter()
        for p, r in zip(parsed, reasons):
            cells[(p.get("price_band", "?"), r)] += 1
        enabled = Counter(bool(i.get("enabled")) for i in items)
        report["scenarios"][name] = {
            "count": len(items),
            "enabled": {str(k): v for k, v in enabled.items()},
            "turns": dict(Counter(len(i.get("turns", [])) for i in items)),
            "variables": {
                key: dict(Counter(p.get(key, "未解析") for p in parsed))
                for key in ("price_band", "brand", "body", "energy", "people")
            },
            "reject_reason": dict(Counter(reasons)),
            "strata_price_x_reason": {
                f"{band}|{reason}": n for (band, reason), n in sorted(cells.items())
            },
            "min_cell": min(cells.values()) if cells else 0,
            "min_cell_count": len(cells),
        }
    return report


def _assert_words(item: dict) -> list[tuple[str, str]]:
    """抽出该题断言里的 (类型, 关键词)。"""
    out: list[tuple[str, str]] = []
    for turn in item.get("turns", []):
        for assertion in turn.get("assert") or []:
            kind = assertion.get("type", "")
            word = assertion.get("word")
            if word:
                out.append((kind, str(word)))
    return out


def check_asserts(corpus: dict, prices: dict[str, list[float]]) -> dict:
    """断言的**误伤面**：`not_mentions '12'` 这类裸字符串断言，若答案里合法地出现
    「12」（如 12.18 万的车价），会把正确答案判成失败。

    `prices` = 价位段 → 该价位的真实价格列表（来自库）。
    """
    out: dict = {"unsafe_questions": [], "checked": 0}
    for item in corpus.get("scenarios", []):
        for turn in item.get("turns", []):
            for assertion in turn.get("assert") or []:
                if assertion.get("type") != "not_mentions":
                    continue
                word = str(assertion.get("word") or "")
                if not word:
                    continue
                band = _parse_turn1(item["turns"][0]["user"]).get("price_band", "")
                band_prices = prices.get(band, [])
                hits = [p for p in band_prices if word in _price_text(p)]
                out["checked"] += 1
                if hits:
                    out["unsafe_questions"].append(
                        {
                            "id": item.get("id"),
                            "scenario": item.get("scenario"),
                            "forbidden_word": word,
                            "price_band": band,
                            "colliding_prices": hits[:5],
                        }
                    )
    return out


def _price_text(price: float) -> str:
    """价格在答案里的两种写法（万元 / 元）都算命中。"""
    return f"{price / 10000:g} {price:g}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多轮题库变量分布审计（计划 §七）")
    parser.add_argument("--corpus", default="eval/multiturn.json")
    parser.add_argument("--check-asserts", action="store_true", help="检查断言的误伤面（需要库）")
    parser.add_argument("--db", default=None, help="检查断言时用的库（默认走 DATABASE_URL）")
    parser.add_argument("--json", dest="json_out", default=None, help="把报告写成 JSON")
    args = parser.parse_args(argv)

    path = Path(args.corpus)
    if not path.exists():
        print(f"题库不存在：{path}\n（本地专属文件，先跑 tools/gen_eval_questions.py 生成）")
        return 2
    corpus = json.loads(path.read_text(encoding="utf-8"))

    report = audit(corpus)
    for name, body in report["scenarios"].items():
        print(f"\n=== {name}：{body['count']} 题  轮次 {body['turns']}  enabled {body['enabled']}")
        for key, dist in body["variables"].items():
            print(f"  {key}: {len(dist)} 种 → {dist}")
        print(f"  拒绝理由: {body['reject_reason']}")
        print(
            f"  分层（价位段×理由）: {body['min_cell_count']} 格，"
            f"最小格 {body['min_cell']} 题"
        )

    if args.check_asserts:
        from app.common.database import get_session_factory

        bands = _bands_from_corpus(corpus)
        with get_session_factory()() as db:
            prices = _prices_by_band(db, bands)
        asserts = check_asserts(corpus, prices)
        report["asserts"] = asserts
        print(
            f"\n=== 断言误伤面：检查 {asserts['checked']} 条 not_mentions 断言，"
            f"其中 {len(asserts['unsafe_questions'])} 条**会误伤正确答案**"
        )
        for row in asserts["unsafe_questions"][:10]:
            print(
                f"  {row['id']} 禁词「{row['forbidden_word']}」"
                f"（{row['price_band']}）→ 合法价格也会命中：{row['colliding_prices']}"
            )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\n报告：{args.json_out}")
    return 0


def _bands_from_corpus(corpus: dict) -> dict[str, tuple[float, float]]:
    """从题库里抽出价位段（不写死——写死就会与题库漂移）。"""
    bands: dict[str, tuple[float, float]] = {}
    for item in corpus.get("scenarios", []):
        for turn in item.get("turns", []):
            band = _parse_turn1(turn["user"]).get("price_band")
            if not band:
                continue
            lo, hi = band.replace("万", "").split("-")
            bands[band] = (float(lo) * 10000, float(hi) * 10000)
    return bands


def _prices_by_band(db, bands: dict[str, tuple[float, float]]) -> dict[str, list[float]]:
    """各价位段在库里的真实价格（传入 Session，便于测试用夹具库）。"""
    from sqlalchemy import select

    from app.common.models import OfficialPrice, VehicleVariant

    out: dict[str, list[float]] = {k: [] for k in bands}
    for series_id, price in db.execute(
        select(VehicleVariant.series_id, OfficialPrice.price_cny)
        .join(OfficialPrice, OfficialPrice.variant_id == VehicleVariant.id)
        .where(VehicleVariant.status == "on_sale")
    ).all():
        if price is None:
            continue
        for band, (lo, hi) in bands.items():
            if lo <= float(price) <= hi:
                out[band].append(float(price))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
