"""取舍叙事的**正确渲染率**（提案 §5 第二条硬判据）。

## 为什么必须有这个指标

取舍叙事是「像真实销售」最直接的那句话——`engine._recommend_reply` 会把它渲染成
「注意妥协项：空间不及本批最优候选」。它是**用户唯一能看见的、模型会参与生成的
推荐理由**，而且天然是**负面断言**（「这台不如别的车」）。

负面断言是幻觉最容易藏身的地方。所以它必须有指标，否则「L3 已修活」只是
「有输出」，不是「输出可信」。

## 判据怎么独立

判据不是「再调一次 `_tradeoff_gaps`」（那等于自己判自己）。它对**每一条**宣称的
取舍逐条核对三个**可由数据推翻**的条件：

1. **该维度有库内实值**——`dim ∈ _measured`。否则说「不及最优」就是
   **用缺失数据编造负面事实**（space/power 的 0.5、comfort/intelligence 的 0.0
   都表示库内没这项数据）；
2. **该维度参与加权**——`dim ∈ weights`；没加权的维度不进 best_by_dim；
3. **真的落后且达到阈值**——`best_by_dim[dim] - _dims[dim] >= _TRADEOFF_GAP`。
   低于阈值是噪声，报出来只会稀释真正的取舍。

任何一条不成立 → 该条判定为**编造**，计入失败。

**诚实边界**：逐维得分本身仍来自生产评分器，本判据复核的是**断言层**
（有没有拿缺失数据说事、有没有把噪声当取舍），不是重新实现评分。
评分层要独立复核得重写一遍 8 维公式，那是另一件事。

## 跑法

    python tools/eval_tradeoffs.py

用内存库 + 手工构造的对照组（每台车只在**一个**维度上刻意落后，且缺失数据的
维度专门单列一组），**不依赖外部数据、不需要 LLM**。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.agent.schemas import Budget, UserProfile  # noqa: E402
from app.agent.tools import (  # noqa: E402
    _DIM_LABELS,
    _TRADEOFF_GAP,
    DEFAULT_WEIGHTS,
    recommendation_tool,
)
from app.common import models  # noqa: E402,F401  确保模型注册
from app.common.database import Base  # noqa: E402

LABEL_TO_DIM = {v: k for k, v in _DIM_LABELS.items()}
SUFFIX = "不及本批最优候选"


def _seed(db: Session) -> None:
    """构造一批**刻意可判**的候选。

    ⚠️ 第一版这组种子**测不到任何东西**（反向验证实测：拆掉「有实值」与
    「达阈值」两道防线，判据仍报 100%）。两个原因，都写在这里免得后人重踩：

    1. `comfort` / `intelligence` 只有在事实的**类别**命中
       `_COMFORT_CATEGORIES` / `_INTELLIGENCE_CATEGORIES` 时才算 measured。
       当时随手写的类别「配置信息」不被识别 → 这两个维度**全批都没测到**，
       于是「无配置车」那台根本没进防线覆盖区。
    2. 没有**亚阈值**的差：所有差距都是大跳，`_TRADEOFF_GAP` 这道闸门形同虚设。

    现在按缺陷反着造：
    - `缺车长车` / `缺舒适车`：对应维度**库内无数据** → 不得被说成落后；
    - `微短车`：比基线**短** 50mm → 空间差 0.056 < 阈值 0.2 → 属噪声，不得报；
    - `弱动车`：真正大幅落后 → **应当**被报出来（判据不能只测「不报」）。
    """
    from tests.seed import make_brand, make_series, make_source, make_variant, make_year

    source = make_source(db, name="评测来源")
    brand = make_brand(db, name="评测品牌", source=source)

    def comfort_fact() -> tuple:
        return ("舒适性", "座椅通风", "有", None, None)

    def intel_fact() -> tuple:
        return ("智能座舱", "自动泊车", "有", None, None)

    # (车系名, 车长mm 或 None, 功率kW, 是否带舒适事实, 是否带智能事实)
    plan = [
        ("基线车", 4800, 250, True, True),    # 各维度都不差 → 应「无明显妥协」
        ("微短车", 4750, 250, True, True),    # 比基线短 50mm → 空间差 0.056 < 阈值
        ("缺车长车", None, 250, True, True),  # 空间**无库内数据** → 不得报
        ("缺舒适车", 4800, 250, False, True), # 舒适**无库内数据** → 不得报
        ("弱动车", 4800, 100, True, True),    # 动力大幅落后 → **应当**报
    ]
    for name, length, power, has_comfort, has_intel in plan:
        s = make_series(db, brand, name=name, body_type="suv", energy_types=("BEV",), source=source)
        y = make_year(db, s)
        facts: list[tuple] = []
        if length is not None:
            facts.append(("参数信息", "长度(mm)", str(length), "mm", None))
        if power is not None:
            facts.append(("参数信息", "最大功率(kW)", str(power), "kW", None))
        if has_comfort:
            facts.append(comfort_fact())
        if has_intel:
            facts.append(intel_fact())
        make_variant(db, s, y, price_cny="150000", facts=facts, source=source)
    db.commit()


def judge(result: dict, profile: UserProfile) -> dict:
    """逐条复核每个候选宣称的取舍。返回统计与失败明细。"""
    weights = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (profile.weights or {}).items()
                                     if v is not None}}
    variants = result.get("variants") or []

    # best_by_dim 独立重算：不复用被测代码里的那份
    best_by_dim: dict[str, float] = {}
    for v in variants:
        for d in (v.get("_measured") or ()):
            if d in weights:
                best_by_dim[d] = max(best_by_dim.get(d, 0.0), (v.get("_dims") or {}).get(d, 0.0))

    total = 0
    ok = 0
    failures: list[dict] = []
    for v in variants:
        dims = v.get("_dims") or {}
        measured = set(v.get("_measured") or ())
        for claim in v.get("tradeoffs") or []:
            total += 1
            reasons: list[str] = []
            if not claim.endswith(SUFFIX):
                reasons.append(f"文案格式不认识：{claim!r}")
            label = claim[: -len(SUFFIX)] if claim.endswith(SUFFIX) else claim
            dim = LABEL_TO_DIM.get(label)
            if dim is None:
                reasons.append(f"维度名 {label!r} 不在 _DIM_LABELS 里（凭空发明的维度）")
            else:
                if dim not in measured:
                    reasons.append(f"{dim} 无库内实值却声称落后 —— 用缺失数据编造负面事实")
                if dim not in weights:
                    reasons.append(f"{dim} 未参与加权，不该出现在取舍里")
                elif best_by_dim.get(dim, 0.0) - dims.get(dim, 0.0) < _TRADEOFF_GAP:
                    reasons.append(
                        f"{dim} 落后 {best_by_dim.get(dim, 0.0) - dims.get(dim, 0.0):.4f} "
                        f"< 阈值 {_TRADEOFF_GAP} —— 噪声被当取舍"
                    )
            if reasons:
                failures.append({"series": v.get("series_name"), "claim": claim, "why": reasons})
            else:
                ok += 1
    return {
        "candidates": len(variants),
        "claims": total,
        "verified": ok,
        "rate": round(ok / total, 4) if total else 1.0,
        "failures": failures,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="取舍叙事正确渲染率（提案 §5 判据 2）")
    ap.add_argument("--json", help="结果写入该路径")
    args = ap.parse_args()

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)
        profile = UserProfile(budget=Budget(min=100000, max=200000))
        res = recommendation_tool(db, profile, limit=10, include_dims=True)
        # 默认路径必须不含私有键——顺手验一次「include_dims 只是评测开关」
        res_default = recommendation_tool(db, profile, limit=10)
        leaked = [k for v in (res_default.get("variants") or []) for k in v
                  if k in ("_dims", "_measured")]

    rep = judge(res, profile)
    print(f"候选 {rep['candidates']} 台，宣称取舍 {rep['claims']} 条")
    print(f"正确渲染率：{rep['verified']}/{rep['claims']} = {rep['rate']:.0%}")
    if leaked:
        print(f"\n❌ 默认路径泄漏了私有键 {sorted(set(leaked))} —— include_dims 破坏了「默认不变」")
    else:
        print("默认路径不含 _dims/_measured（include_dims 未污染生产响应体）✓")

    for v in res["variants"]:
        if v.get("tradeoffs"):
            print(f"  {v['series_name']}: {'；'.join(v['tradeoffs'])}")
        else:
            print(f"  {v['series_name']}: 无明显妥协")

    if rep["failures"]:
        print(f"\n❌ 编造 {len(rep['failures'])} 条：")
        for f in rep["failures"]:
            print(f"  [{f['series']}] {f['claim']}\n      {'；'.join(f['why'])}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({k: v for k, v in rep.items() if k != "failures"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    verdict = rep["rate"] == 1.0 and not leaked
    print(f"\n硬判据 2（取舍叙事正确渲染率）：{'通过' if verdict else '不通过'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
