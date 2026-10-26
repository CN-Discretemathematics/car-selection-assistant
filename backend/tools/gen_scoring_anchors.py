"""生成评分 v2 的版本化锚点（同价位分位）。

用法（在 backend/ 下）：
    python tools/gen_scoring_anchors.py --db ../backend/.tmp/eval-local.db \
        --out app/agent/scoring_anchors_v2.json \
        --anchor-version scoring-v2-2026-10-03 --catalog-date 2026-10-03

锚点 = 全局分位（全库在售款型）+ 每个价格带的带内分位；带内样本 < --min-band-samples
时该带/该指标回退全局。输出 p10/p50/p90 与样本数，文件提交 git，目录刷新后重跑。

设计：docs/2026-10-27-scoring-v2-design.md §6；本脚本只读数据库。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path)

from app.agent import scoring_v2  # noqa: E402
from app.agent.weights import PRICE_BANDS_V2  # noqa: E402

#: (维度, 子键, 取值函数, 是否计数值, 过滤函数)
MEASURES: tuple[tuple[str, str, Callable[[Mapping[str, Any]], float | None], bool, Callable[[Mapping[str, Any]], bool]], ...] = (
    ("space", "wheelbase", lambda f: f["space"]["wheelbase"], False, lambda f: True),
    ("space", "length", lambda f: f["space"]["length"], False, lambda f: True),
    ("space", "width", lambda f: f["space"]["width"], False, lambda f: True),
    ("space", "trunk", lambda f: f["space"]["trunk"], False, lambda f: True),
    ("power", "kw_per_t", lambda f: f["power"]["kw_per_t"], False, lambda f: True),
    ("running_cost", "cny_per_100km", lambda f: f["running_cost"]["cny_per_100km"], False, lambda f: True),
    ("safety", "count", lambda f: f["safety_count"], True, lambda f: f["safety_fact_count"] >= 5),
    ("comfort", "count", lambda f: f["comfort_count"], True, lambda f: f["comfort_fact_count"] >= 1),
    ("intelligence", "count", lambda f: f["intelligence_count"], True, lambda f: f["intelligence_fact_count"] >= 1),
    ("usage", "length", lambda f: f["space"]["length"], False, lambda f: True),
    ("usage", "trunk", lambda f: f["space"]["trunk"], False, lambda f: True),
    ("usage", "range", lambda f: f["range_km"], False, lambda f: True),
    ("usage", "intelligence", lambda f: f["intelligence_count"], True, lambda f: f["intelligence_fact_count"] >= 1),
)


def _sqlite_connect(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_rows(db: str | None, database_url: str | None) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], str]:
    if db:
        conn = _sqlite_connect(db)
        try:
            variant_rows = conn.execute(
                """
                SELECT v.id AS variant_id, v.series_id, v.energy_type, v.body_type,
                       v.seat_count, p.price_cny, p.effective_from
                FROM vehicle_variants v
                JOIN official_prices p ON p.variant_id = v.id AND p.effective_to IS NULL
                WHERE v.status = 'on_sale'
                """
            ).fetchall()
            ids = [int(r["variant_id"]) for r in variant_rows]
            fact_rows: list[Mapping[str, Any]] = []
            for start in range(0, len(ids), 900):
                chunk = ids[start : start + 900]
                qmarks = ",".join("?" * len(chunk))
                fact_rows.extend(
                    conn.execute(
                        f"SELECT variant_id, category, fact_key, fact_value, unit, cycle "
                        f"FROM spec_facts WHERE variant_id IN ({qmarks})",
                        chunk,
                    ).fetchall()
                )
        finally:
            conn.close()
        return [dict(r) for r in variant_rows], [dict(r) for r in fact_rows], f"sqlite:{Path(db).name}"

    if not database_url:
        raise SystemExit("必须提供 --db（SQLite 快照）或 --database-url（PostgreSQL）")
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = database_url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as conn:
        variant_rows = [
            dict(row)
            for row in conn.execute(
                text(
                    """
                    SELECT v.id AS variant_id, v.series_id, v.energy_type, v.body_type,
                           v.seat_count, p.price_cny, p.effective_from
                    FROM vehicle_variants v
                    JOIN official_prices p ON p.variant_id = v.id AND p.effective_to IS NULL
                    WHERE v.status = 'on_sale'
                    """
                )
            ).mappings()
        ]
        ids = [int(r["variant_id"]) for r in variant_rows]
        fact_rows = []
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            if not chunk:
                continue
            fact_rows.extend(
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT variant_id, category, fact_key, fact_value, unit, cycle "
                        "FROM spec_facts WHERE variant_id = ANY(:ids)"
                    ),
                    {"ids": chunk},
                ).mappings()
            )
    parsed = make_url(database_url)
    return variant_rows, fact_rows, f"postgresql:{parsed.host or '?'}/{parsed.database or '?'}"


def _dedupe_latest_prices(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    latest: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        vid = int(row["variant_id"])
        current = latest.get(vid)
        if current is None or str(row.get("effective_from") or "") >= str(current.get("effective_from") or ""):
            latest[vid] = row
    return list(latest.values())


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = pos - low
    return sorted_values[low] * (1.0 - fraction) + sorted_values[high] * fraction


def _anchor(values: list[float], *, is_count: bool) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    p10 = _quantile(ordered, 0.10)
    p50 = _quantile(ordered, 0.50)
    p90 = _quantile(ordered, 0.90)
    if is_count:
        if p10 >= p50:
            p10 = max(0.0, p50 - 1.0)
        if p90 <= p50:
            p90 = p50 + 1.0
    else:
        span = max(abs(p50) * 0.01, 1e-6)
        if p10 >= p50:
            p10 = p50 - span
        if p90 <= p50:
            p90 = p50 + span
    if not (p10 < p50 < p90):
        return None
    return {"p10": round(p10, 4), "p50": round(p50, 4), "p90": round(p90, 4), "n": len(values)}


def _build_anchor_block(
    features: list[Mapping[str, Any]],
    *,
    min_samples: int,
) -> tuple[dict[str, Any], dict[str, int], dict[str, int]]:
    """返回 (anchors, n_by_subkey, omitted)。"""
    values: dict[str, list[float]] = defaultdict(list)
    for feature in features:
        for dim, subkey, getter, _is_count, predicate in MEASURES:
            if not predicate(feature):
                continue
            raw = getter(feature)
            if raw is None:
                continue
            try:
                values[f"{dim}.{subkey}"].append(float(raw))
            except (TypeError, ValueError):
                continue
    anchors: dict[str, Any] = {}
    counts: dict[str, int] = {}
    omitted: dict[str, int] = {}
    for dim, subkey, _getter, is_count, _predicate in MEASURES:
        key = f"{dim}.{subkey}"
        series = values.get(key, [])
        counts[key] = len(series)
        if len(series) < min_samples:
            omitted[key] = len(series)
            continue
        built = _anchor(series, is_count=is_count)
        if built is None:
            omitted[key] = len(series)
            continue
        anchors.setdefault(dim, {})[subkey] = built
    return anchors, counts, omitted


def _validate(anchors: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for scope_name, block in (("global", anchors.get("global") or {}),):
        for dim, subkeys in block.items():
            for subkey, item in subkeys.items():
                if not (item["p10"] < item["p50"] < item["p90"]):
                    problems.append(f"{scope_name}.{dim}.{subkey} p10<p50<p90 不成立: {item}")
    for band_id, band in (anchors.get("by_price_band") or {}).items():
        for dim, subkeys in (band.get("anchors") or {}).items():
            for subkey, item in subkeys.items():
                if not (item["p10"] < item["p50"] < item["p90"]):
                    problems.append(f"band{band_id}.{dim}.{subkey} p10<p50<p90 不成立")
    return problems


def _check_drift(current: Mapping[str, Any], previous: Mapping[str, Any], threshold: float = 0.10) -> list[str]:
    warnings: list[str] = []
    previous_global = previous.get("global") or {}
    for dim, subkeys in (current.get("global") or {}).items():
        for subkey, item in subkeys.items():
            old = ((previous_global.get(dim) or {}).get(subkey) or {})
            old_p50 = old.get("p50")
            if not old_p50:
                continue
            change = abs(float(item["p50"]) - float(old_p50)) / max(abs(float(old_p50)), 1e-9)
            if change > threshold:
                warnings.append(f"{dim}.{subkey} p50 {old_p50} -> {item['p50']} 变化 {change:.1%}")
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="生成评分 v2 锚点 JSON")
    parser.add_argument("--db", help="SQLite 快照路径（只读）")
    parser.add_argument("--database-url", help="PostgreSQL 连接串（生产只读账号）")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "app" / "agent" / "scoring_anchors_v2.json"))
    parser.add_argument("--anchor-version", default=scoring_v2.ANCHOR_VERSION)
    parser.add_argument("--catalog-date", default="")
    parser.add_argument("--source-label", default="")
    parser.add_argument("--min-band-samples", type=int, default=30)
    parser.add_argument("--previous", help="旧锚点文件，用于 p50 漂移检查")
    parser.add_argument("--force", action="store_true", help="漂移超阈值仍写入")
    args = parser.parse_args()

    variant_rows, fact_rows, source = _fetch_rows(args.db, args.database_url)
    variant_rows = _dedupe_latest_prices(variant_rows)
    facts_by_variant: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for fact in fact_rows:
        facts_by_variant[int(fact["variant_id"])].append(fact)

    features: list[Mapping[str, Any]] = []
    skipped_no_price = 0
    for row in variant_rows:
        try:
            price = float(row["price_cny"])
        except (TypeError, ValueError, KeyError):
            skipped_no_price += 1
            continue
        variant = SimpleNamespace(
            id=int(row["variant_id"]),
            series_id=row["series_id"],
            energy_type=row["energy_type"],
            body_type=row["body_type"],
            seat_count=row["seat_count"],
        )
        features.append(scoring_v2.extract_features(variant, facts_by_variant[int(row["variant_id"])], price))

    global_anchors, global_n, global_omitted = _build_anchor_block(features, min_samples=args.min_band_samples)
    by_band: dict[str, Any] = {}
    band_features: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for feature in features:
        band_idx = scoring_v2.price_band_index(feature.get("price_cny"))
        if band_idx is not None:
            band_features[band_idx].append(feature)
    for idx, (low, high) in enumerate(PRICE_BANDS_V2):
        rows_in_band = band_features.get(idx, [])
        if len(rows_in_band) < args.min_band_samples:
            by_band[str(idx)] = {
                "price_band_wan": [low, high],
                "n_total": len(rows_in_band),
                "fallback": True,
                "anchors": {},
                "n_by_subkey": {},
                "fallback_subkeys": {key: len(rows_in_band) for key in global_n},
            }
            continue
        anchors, n_by_subkey, omitted = _build_anchor_block(rows_in_band, min_samples=args.min_band_samples)
        by_band[str(idx)] = {
            "price_band_wan": [low, high],
            "n_total": len(rows_in_band),
            "fallback": False,
            "anchors": anchors,
            "n_by_subkey": n_by_subkey,
            "fallback_subkeys": omitted,
        }

    payload: dict[str, Any] = {
        "anchor_version": args.anchor_version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": args.source_label or source,
        "catalog_date": args.catalog_date,
        "price_bands_wan": [list(band) for band in PRICE_BANDS_V2],
        "min_band_samples": args.min_band_samples,
        "method": "p10/p50/p90 linear-interpolated quantiles over on-sale variants with current official price",
        "variant_count": len(features),
        "skipped_no_price": skipped_no_price,
        "global": global_anchors,
        "global_n_by_subkey": global_n,
        "global_fallback_subkeys": global_omitted,
        "by_price_band": by_band,
    }

    problems = _validate(payload)
    if problems:
        print("锚点校验失败:", file=sys.stderr)
        for problem in problems:
            print(" -", problem, file=sys.stderr)
        return 2

    if args.previous:
        previous_path = Path(args.previous)
        if previous_path.exists():
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            drift = _check_drift(payload, previous)
            if drift:
                print("锚点漂移超过 10%:", file=sys.stderr)
                for item in drift:
                    print(" -", item, file=sys.stderr)
                if not args.force:
                    print("确认无误后加 --force 重新生成", file=sys.stderr)
                    return 3

    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {out_path}（{len(features)} 款在售，{len(by_band)} 个价格带）")
    for idx in range(len(PRICE_BANDS_V2)):
        band = by_band[str(idx)]
        flag = " FALLBACK" if band["fallback"] else ""
        print(f"  band {idx} {band['price_band_wan']}: n={band['n_total']}{flag}")
    for dim, subkeys in sorted(global_anchors.items()):
        for subkey, item in sorted(subkeys.items()):
            print(f"  global {dim}.{subkey}: p10={item['p10']} p50={item['p50']} p90={item['p90']} n={item['n']}")
    if skipped_no_price:
        print(f"  跳过无当前价格款型: {skipped_no_price}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
