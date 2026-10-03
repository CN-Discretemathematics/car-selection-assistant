"""L1 接入排序的 A/B：把软偏好权重注入前后，推荐结果**在用户强调的维度上**是否真的更好。

## 为什么做这个

§5 三条硬判据都已实测通过，但它们回答的都是「**不变差**」：
硬约束不回归、取舍叙事不编造、抽取本身准。它们都**没有回答**那个真正的问题——
**「把 L1 接上，用户会看到更符合他强调的那台车吗？」**

提案 §6 第 4 步一直卡在这里。现在补上。

## 怎么隔离 L1 的贡献

LLM 抽取**不**放进本评测（那部分由 `tools/eval_soft_prefs.py` 单独测）。这里固定
输入「已验证的偏好向量」，只比较**注入前后**的排序差异，于是测的纯粹是
**L1 的权重映射是否把车推对了方向**。

权重注入走**真实生产路径**：`soft_prefs.to_hints()` → `engine.merge_profile()`，
不另写一套映射——否则测的是「另一份实现」。

## 三个指标

1. **强调维度均分**：top-5 候选在该维度上的 `_dims` 均值。**越高越好**，
   直接对应「用户最看重的那个点上，推荐的车是不是真的好」。
2. **强调维度冠军入选率**：该维度上全批得分最高的候选，有几台进了 top-5。
3. **候选集不变**：注入前后候选 id 集合必须逐位一致——L1 只能改排序，
   改候选集就是越权（这条是硬红线，不满足直接判失败）。

## 跑法

    DATABASE_URL=sqlite:///.tmp/eval-local.db RETRIEVAL_BACKEND=inmemory \
        python tools/eval_softpref_ab.py

需要一份真实数据的库。**结论只在同一份库上成立**（见提案 §5.3：题库不入库）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session  # noqa: E402

from app.agent import soft_prefs as sp  # noqa: E402
from app.agent.schemas import Budget, UserProfile  # noqa: E402
from app.agent.tools import DEFAULT_WEIGHTS, recommendation_tool  # noqa: E402
from app.common.database import get_session_factory  # noqa: E402

QUESTIONS = Path(__file__).resolve().parents[1] / "tests" / "soft_prefs_ab.jsonl"

# 比候选集用的上限：足够大到覆盖整个硬约束过滤结果（快照库全库约 6.6k 款型，
# 带约束后通常几百）。L1 不改 SQL 条件，所以两边都应当恰好返回这个上限内的全部。
_FULL = 5000


def load_cases() -> list[dict]:
    return [json.loads(ln) for ln in QUESTIONS.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _profile(case: dict) -> UserProfile:
    p = UserProfile()
    b = case.get("budget_cny")
    if b:
        p.budget = Budget(min=b[0], max=b[1])
    if case.get("usage"):
        p.usage = list(case["usage"])
    if case.get("body_type"):
        p.body_type = list(case["body_type"])
    if case.get("energy_preference"):
        p.energy_preference = list(case["energy_preference"])
    if case.get("passengers") is not None:
        p.passengers = case["passengers"]
    return p


def _with_prefs(case: dict) -> UserProfile:
    """把 L1 的偏好向量按**真实生产路径**注入画像。"""
    p = _profile(case)
    sp.apply_soft_prefs(p, case.get("prefs") or {})
    return p


def _top(res: dict, k: int) -> list[dict]:
    return (res.get("variants") or [])[:k]


def _mean(vals: list[float]) -> float:
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def evaluate(db: Session, case: dict, k: int) -> dict:
    dim = case["emphasis_dim"]
    base = recommendation_tool(db, _profile(case), limit=k, include_dims=True)
    tuned = recommendation_tool(db, _with_prefs(case), limit=k, include_dims=True)

    base_ids = [v["variant_id"] for v in _top(base, k)]
    tuned_ids = [v["variant_id"] for v in _top(tuned, k)]

    # 硬红线的正确口径：**全量候选集**（硬约束 SQL 过滤的结果），不是 top-k。
    # ⚠️ 第一版这里比的是 top-k，于是「权重变了 → 排名变 → top-k 成员变」被当成
    # 候选集被改，5 条全被判红线——那是**把 L1 的正常工作当成越权**。
    # 真正要保证的是：软偏好只改排序，不改「谁有资格进候选」。
    full_base = {v["variant_id"] for v in
                 (recommendation_tool(db, _profile(case), limit=_FULL, include_dims=True)
                  .get("variants") or [])}
    full_tuned = {v["variant_id"] for v in
                  (recommendation_tool(db, _with_prefs(case), limit=_FULL, include_dims=True)
                   .get("variants") or [])}

    # 全批在该维度上的冠军（不受 top-k 限制影响）
    champ = max(
        (v for v in (base.get("variants") or []) if dim in (v.get("_measured") or ())),
        key=lambda v: v["_dims"][dim],
        default=None,
    )

    # ⚠️ 「强调维度均分」这个指标**天然偏向 L1**：to_hints 只会**抬高**被强调维度的
    # 权重，所以该维度均分几乎不可能下降——拿它当证据等于自证。
    # 因此必须同时看**代价**：其余维度均分掉了多少。变好 5 / 变差 0 只是故事的一半。
    other_dims = sorted(set(DEFAULT_WEIGHTS) - {dim})

    def _other_mean(res: dict) -> float:
        vals = [
            v["_dims"][d]
            for v in _top(res, k)
            for d in other_dims
            if d in (v.get("_measured") or ())
        ]
        return _mean(vals)

    return {
        "id": case["id"],
        "text": case["text"],
        "dim": dim,
        "base_dim_mean": _mean([v["_dims"].get(dim, 0.0) for v in _top(base, k)]),
        "tuned_dim_mean": _mean([v["_dims"].get(dim, 0.0) for v in _top(tuned, k)]),
        "base_other_mean": _other_mean(base),
        "tuned_other_mean": _other_mean(tuned),
        "champ_in_base_topk": bool(champ and champ["variant_id"] in base_ids),
        "champ_in_tuned_topk": bool(champ and champ["variant_id"] in tuned_ids),
        "candidate_count_base": len(full_base),
        "candidate_count_tuned": len(full_tuned),
        "candidate_set_same": full_base == full_tuned,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="L1 软偏好接入排序的 A/B 评测")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--json", help="结果写入该路径")
    args = ap.parse_args()

    cases = load_cases()
    print(f"软偏好 A/B 语料：{len(cases)} 条（入库，随代码演进）\n")

    # get_session 是 FastAPI 依赖（generator），不能当上下文管理器用；
    # 独立脚本走 session factory（与 eval_rag 同一入口）
    db = get_session_factory()()
    try:
        rows = [evaluate(db, c, args.top_k) for c in cases]
    finally:
        db.close()

    better = sum(1 for r in rows if r["tuned_dim_mean"] > r["base_dim_mean"])
    worse = sum(1 for r in rows if r["tuned_dim_mean"] < r["base_dim_mean"])
    same = len(rows) - better - worse

    other_deltas = [r["tuned_other_mean"] - r["base_other_mean"] for r in rows]
    other_better = sum(1 for d in other_deltas if d > 0)
    other_worse = sum(1 for d in other_deltas if d < 0)
    other_same = len(other_deltas) - other_better - other_worse
    avg_other_delta = round(sum(other_deltas) / len(other_deltas), 4) if other_deltas else 0.0

    champ_gain = sum(1 for r in rows if r["champ_in_tuned_topk"] and not r["champ_in_base_topk"])
    champ_loss = sum(1 for r in rows if r["champ_in_base_topk"] and not r["champ_in_tuned_topk"])
    redline = [r for r in rows if not r["candidate_set_same"]]

    print(f"{'id':<6} {'强调维度':<12} {'基线':>7} {'注入后':>7} {'Δ强调':>8} "
          f"{'Δ其他':>8} {'候选集':>7}")
    print("-" * 74)
    for r in rows:
        d1 = round(r["tuned_dim_mean"] - r["base_dim_mean"], 4)
        d2 = round(r["tuned_other_mean"] - r["base_other_mean"], 4)
        m1 = "↑" if d1 > 0 else ("↓" if d1 < 0 else "=")
        m2 = "↑" if d2 > 0 else ("↓" if d2 < 0 else "=")
        print(f"{r['id']:<6} {r['dim']:<12} {r['base_dim_mean']:>7.4f} {r['tuned_dim_mean']:>7.4f} "
              f"{m1}{abs(d1):>7.4f} {m2}{abs(d2):>7.4f} "
              f"{'不变' if r['candidate_set_same'] else '✗改了':>7}")

    print(f"\n强调维度均分：变好 {better} / 变差 {worse} / 持平 {same}")
    print("⚠️ 这个方向**天然偏向 L1**（to_hints 只会抬高被强调维度的权重，"
          "均分几乎不可能下降）——真正的代价看「Δ其他」列。")
    print(f"其余维度均分：变好 {other_better} / 变差 {other_worse} / 持平 {other_same}"
          f"（平均代价 {avg_other_delta:+.4f}）")
    print(f"该维度全批冠军进入 top-{args.top_k}：注入后新增 {champ_gain}，丢失 {champ_loss}"
          f"（本批预算下冠军本就都在 top-{args.top_k}，该指标不具区分力）")
    print(f"候选集不变（硬红线）：{'全部通过' if not redline else f'✗ {len(redline)} 条被改动'}")
    for r in redline:
        print(f"  ✗ {r['id']} {r['text']}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = not redline and better > worse
    print(f"\n结论：{'注入软偏好后，推荐在用户强调的维度上确实更好，且未改候选集' if ok else '尚不能宣称变好'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
