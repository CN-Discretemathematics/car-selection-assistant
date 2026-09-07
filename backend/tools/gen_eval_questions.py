"""生成评测问题库（100~300 条测试问题，按品牌/车系/SKU 分层抽样）。

问题全部锚定数据库中的真实品牌/车系/SKU（生成时抽样，不人工编造事实）；
意图模板覆盖预算/能源/车身/座位/家庭用途/续航/对比/开放追问等类别。
用法：
    python tools/gen_eval_questions.py --count 200 --output eval/questions.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import Brand, VehicleSeries, VehicleVariant  # noqa: E402

INTENT_TEMPLATES = [
    "budget_suv",
    "budget_sedan",
    "budget_mpv",
    "energy_bev",
    "energy_phev_erev",
    "family_seats",
    "commute",
    "long_range_bev",
    "series_spec",
    "brand_series_ask",
    "open_clarify",
    "compare_two",
]


def _wan(price: float | None) -> int:
    return max(int((price or 150000) / 10000) + 1, 3)


def _build_questions(brands, series_by_brand, variants_by_series, price_by_variant, count: int) -> list[dict]:
    rng = random.Random(20260829)  # 固定种子：问题库可复现
    questions: list[dict] = []

    def add(q: dict) -> None:
        q["id"] = f"q{len(questions) + 1:03d}"
        questions.append(q)

    # 分层抽样：每品牌类别按比例取品牌，品牌内取车系，车系内取在售款型
    brand_pool = [b for b in brands if series_by_brand.get(b.id)]
    rng.shuffle(brand_pool)
    per_series_variant: dict[int, list[VehicleVariant]] = {
        sid: [v for v in vs if v.status == "on_sale"] for sid, vs in variants_by_series.items()
    }
    usable_series = [s for lst in series_by_brand.values() for s in lst]
    usable_series = [s for s in usable_series if per_series_variant.get(s.id)]

    while len(questions) < count:
        brand = rng.choice(brand_pool)
        brand_series = [s for s in series_by_brand.get(brand.id, []) if per_series_variant.get(s.id)]
        if not brand_series:
            continue
        series = rng.choice(brand_series)
        variants = per_series_variant[series.id]
        intent = rng.choice(INTENT_TEMPLATES)

        if intent == "budget_suv":
            target = rng.choice([s for s in brand_series if s.body_type == "suv"] or brand_series)
            vlist = [v for v in per_series_variant.get(target.id, []) if v.energy_type == "BEV"] or per_series_variant.get(target.id, [])
            v = rng.choice(vlist)
            budget = _wan(price_by_variant.get(v.id))
            add({"strat": "sku", "intent": intent,
                 "text": f"预算{budget}万，想要一台纯电SUV，家里5口人用，有推荐吗",
                 "anchors": {"brand_name": brand.name, "series_id": target.id, "variant_id": v.id},
                 "expect": {"budget_max": budget * 10000, "energy_type": "BEV", "body_type": "suv", "passengers": 5}})
        elif intent == "budget_sedan":
            target = rng.choice([s for s in brand_series if s.body_type == "sedan"] or brand_series)
            vlist = per_series_variant.get(target.id, [])
            v = rng.choice(vlist)
            budget = _wan(price_by_variant.get(v.id))
            add({"strat": "sku", "intent": intent,
                 "text": f"预算{budget}万以内，想买一台轿车，主要上下班通勤",
                 "anchors": {"brand_name": brand.name, "series_id": target.id, "variant_id": v.id},
                 "expect": {"budget_max": budget * 10000, "body_type": "sedan", "usage": ["通勤"]}})
        elif intent == "budget_mpv":
            target = rng.choice([s for s in brand_series if s.body_type == "mpv"] or brand_series)
            vlist = per_series_variant.get(target.id, [])
            v = rng.choice(vlist)
            budget = _wan(price_by_variant.get(v.id))
            add({"strat": "sku", "intent": intent,
                 "text": f"家里人多，预算{budget}万，想要一台MPV，6个人坐得下吗",
                 "anchors": {"brand_name": brand.name, "series_id": target.id, "variant_id": v.id},
                 "expect": {"budget_max": budget * 10000, "body_type": "mpv", "passengers": 6}})
        elif intent == "energy_bev":
            vlist = [v for v in variants if v.energy_type == "BEV"]
            if not vlist:
                continue
            v = rng.choice(vlist)
            add({"strat": "sku", "intent": intent,
                 "text": f"{brand.name} {series.name} 的纯电版有哪些款型，大概什么价位",
                 "anchors": {"brand_name": brand.name, "series_id": series.id, "variant_id": v.id},
                 "expect": {"energy_type": "BEV", "series_id": series.id}})
        elif intent == "energy_phev_erev":
            vlist = [v for v in variants if v.energy_type in ("PHEV", "EREV")]
            if not vlist:
                continue
            v = rng.choice(vlist)
            add({"strat": "sku", "intent": intent,
                 "text": f"经常跑长途，{brand.name} {series.name} 的插混或增程版值得买吗",
                 "anchors": {"brand_name": brand.name, "series_id": series.id, "variant_id": v.id},
                 "expect": {"energy_preference": ["new_energy"], "series_id": series.id}})
        elif intent == "family_seats":
            v = rng.choice(variants)
            add({"strat": "sku", "intent": intent,
                 "text": f"二胎家庭，5口人，预算20万左右，{brand.name} {series.name} 合适吗",
                 "anchors": {"brand_name": brand.name, "series_id": series.id, "variant_id": v.id},
                 "expect": {"passengers": 5, "budget_max": 200000}})
        elif intent == "commute":
            v = rng.choice(variants)
            add({"strat": "series", "intent": intent,
                 "text": f"每天通勤40公里，想省油省电，{brand.name} {series.name} 怎么样",
                 "anchors": {"brand_name": brand.name, "series_id": series.id},
                 "expect": {"usage": ["通勤"], "series_id": series.id}})
        elif intent == "long_range_bev":
            vlist = [v for v in variants if v.energy_type == "BEV"]
            if not vlist:
                continue
            v = rng.choice(vlist)
            add({"strat": "sku", "intent": intent,
                 "text": f"想要续航500公里以上的纯电车，{brand.name} {series.name} 有吗",
                 "anchors": {"brand_name": brand.name, "series_id": series.id, "variant_id": v.id},
                 "expect": {"energy_type": "BEV", "series_id": series.id}})
        elif intent == "series_spec":
            add({"strat": "series", "intent": intent,
                 "text": f"{brand.name} {series.name} 的官方指导价和车身尺寸是多少",
                 "anchors": {"brand_name": brand.name, "series_id": series.id},
                 "expect": {"series_id": series.id}})
        elif intent == "brand_series_ask":
            add({"strat": "brand", "intent": intent,
                 "text": f"{brand.name} 现在有哪些在售的新能源SUV",
                 "anchors": {"brand_name": brand.name},
                 "expect": {"brand_name": brand.name}})
        elif intent == "open_clarify":
            add({"strat": "brand", "intent": intent,
                 "text": "我想买台车",
                 "anchors": {},
                 "expect": {"need_clarification": True}})
        elif intent == "compare_two":
            other = rng.choice(usable_series)
            va = rng.choice(variants)
            vb_list = per_series_variant.get(other.id, [])
            if not vb_list:
                continue
            vb = rng.choice(vb_list)
            add({"strat": "sku", "intent": intent,
                 "text": f"帮我对比 {va.display_name} 和 {vb.display_name} 的配置差异",
                 "anchors": {"variant_ids": [va.id, vb.id]},
                 "expect": {"variant_ids": [va.id, vb.id]}})
    return questions[:count]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成评测问题库（分层抽样）")
    parser.add_argument("--count", type=int, default=200, help="问题数量（100~300，默认 200）")
    parser.add_argument("--output", default=os.path.join("eval", "questions.json"))
    args = parser.parse_args(argv)
    count = min(max(args.count, 100), 300)

    factory = get_session_factory()
    with factory() as db:
        brands = db.scalars(select(Brand).where(Brand.active_status == "active")).all()
        series_rows = db.scalars(select(VehicleSeries).where(VehicleSeries.active_status == "active")).all()
        variants_rows = db.scalars(
            select(VehicleVariant).where(VehicleVariant.status == "on_sale")
        ).all()
        from app.common.models import OfficialPrice

        price_by_variant = {
            p.variant_id: float(p.price_cny)
            for p in db.scalars(
                select(OfficialPrice).where(OfficialPrice.effective_to.is_(None))
            ).all()
        }

    series_by_brand: dict[int, list[VehicleSeries]] = {}
    for s in series_rows:
        series_by_brand.setdefault(s.brand_id, []).append(s)
    variants_by_series: dict[int, list[VehicleVariant]] = {}
    for v in variants_rows:
        variants_by_series.setdefault(v.series_id, []).append(v)

    questions = _build_questions(brands, series_by_brand, variants_by_series, price_by_variant, count)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "从数据库真实品牌/车系/SKU 分层抽样生成（汽车之家 2026-07 全量数据），非人工编造",
        "count": len(questions),
        "questions": questions,
    }
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"已生成 {len(questions)} 条问题 → {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
