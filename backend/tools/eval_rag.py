"""RAG 检索评测：主流 IR 指标评估切分/召回/融合/重排策略。

黄金集：eval/questions.json（tools/gen_eval_questions.py 从数据库真实数据分层抽样，
anchors.series_id / anchors.variant_id 即相关性判定，无需人工标注）。

指标（主流检索评测口径）：
- HitRate@K   至少一条相关切片进入 top-K 的问题占比（与 eval_agent 的 hit@k 同源可比）；
- Recall@K    |相关∩topK| / |相关|（车系级判定，相关集=该车系全部索引切片）；
- Precision@K |相关∩topK| / K；
- MRR         首条相关切片倒数名次均值；
- NDCG@K      二元相关性折损累计增益。

策略对比（量化每个环节的收益）：
- sparse-nooverlap  BM25 单路，文档切片无重叠   → 切分策略（overlap）收益基线
- sparse            BM25 单路，递归切分+重叠
- dense             Zilliz 向量单路（需 --with-dense 且云端已灌库）
- hybrid            sparse ∥ dense + RRF 融合（需 --with-dense）→ 多路融合收益
- pipeline          完整 LangGraph 流水线（实体解析 + 当前配置重排 + 把关）→ 端到端

用法：
    python tools/eval_rag.py [--limit 50] [--top-k 5] [--with-dense]
    # 本地评测（不触云端）：设 RETRIEVAL_BACKEND=inmemory
退出码：运行失败为 1，评测本身始终返回 0（结果看报告）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path; being an import, it also stops E402 on the app.* imports below)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tools/（复用问题生成器的措辞映射）

from gen_eval_questions import (  # 措辞映射与生成器单一事实源
    HINT_TO_ENERGY,
    HEAD_LABEL_TO_BODY,
    _PARAM_KEYS,
)
from app.catalog.series_constraints import load_series_attrs, series_satisfies as _series_satisfies

from sqlalchemy import select

from app.common.database import get_session_factory
from app.common.models import Brand, OfficialPrice, SourceDocument, SpecFact, VehicleSeries, VehicleVariant
from app.rag.chunking import chunk_stats, split_text
from app.rag.ingest import build_chunks
from app.rag.rerank import PassThroughReranker, rrf_fuse
from app.retrieval.backends import InMemoryRetriever, SearchResult
from app.retrieval.config import CHUNK_OVERLAP, CHUNK_SIZE, RETRIEVAL_BACKEND

EVAL_DEPTH = 10  # 取回深度：指标 @top_k 与 @10 都从这一份排序列表计算


def _portable_anchors(db, questions: list[dict]) -> tuple[list[dict], dict]:
    """把语料的 anchors 从「行号」重解析为「本库真实 id」。

    ## 为什么需要这一步（2026-10-03 实测发现）

    `eval/questions.json` 是 `gen_eval_questions.py` **从当时的数据库分层抽样**生成的，
    `anchors.series_id` / `anchors.variant_id` 写的是那个库的**行号**。行号不是数据，
    换个库（本地快照、另一个环境、重新导入的库）就全错位了，而行号错的**表现极有欺骗性**：
    语料期望 series_id=901「英菲尼迪QX80」，本地导入后 901 恰好存在、却指向「智司LS6」——
    于是检索再完美也判不出相关，Hit@5 变成 0.03。**看着像「检索崩了」，其实是标签指错了人。**

    ## 判据（保守，宁可不动也不要错配）

    1. 语料给的 id 在本库存在，**且该 id 的车系名出现在问句里** → 信任 id（原始库上零变化）；
    2. 否则取「问句里出现的、本库中长度最长的车系名」→ 用它的 id（重解析）；
    3. 都不成立 → 保持原样，并计入 stale，交由调用方告警。

    返回 (questions, stats)。原地改写并返回同一列表是为了不改动下游所有函数签名。
    """
    series = [(int(s.id), str(s.name)) for s in db.scalars(select(VehicleSeries)) if s.name]
    variants = [
        (int(v.id), str(v.display_name))
        for v in db.scalars(select(VehicleVariant))
        if v.display_name
    ]
    name_by_id = dict(series)
    variant_by_id = dict(variants)

    stats = {"remapped": 0, "stale": 0, "total": 0, "sample_old": None, "sample_new": None}

    def _resolve(raw: int, name_map: dict[int, str], candidates: list[tuple[int, str]], text: str) -> int:
        raw = int(raw)
        stats["total"] += 1
        # 判据 1：id 在本库存在且其名称出现在问句里 → 信任 id（在原始库上零变化）
        if raw in name_map and name_map[raw] and name_map[raw] in text:
            return raw
        # 判据 2：取问句里出现的、长度最长的本库名称
        best = None
        for cand_id, cand_name in candidates:
            if len(cand_name) >= 2 and cand_name in text:
                if best is None or len(cand_name) > len(best[1]):
                    best = (cand_id, cand_name)
        if best:
            stats["remapped"] += 1
            if stats["sample_old"] is None:
                stats["sample_old"], stats["sample_new"] = raw, best[0]
            return best[0]
        # 判据 3：保持原样并计入 stale，交由调用方告警
        stats["stale"] += 1
        return raw

    for q in questions:
        text = q.get("text") or ""
        anchors = q.setdefault("anchors", {})
        if anchors.get("series_id"):
            anchors["series_id"] = _resolve(anchors["series_id"], name_by_id, series, text)
        for key in ("variant_id", "variant_ids"):
            value = anchors.get(key)
            if not value:
                continue
            if isinstance(value, list):
                anchors[key] = [_resolve(v, variant_by_id, variants, text) for v in value]
            else:
                anchors[key] = _resolve(value, variant_by_id, variants, text)
    stats["stale_ratio"] = (stats["stale"] / stats["total"]) if stats["total"] else 0.0
    return questions, stats


def _anchor_text_conflicts(
    db, questions: list[dict], variant_series: dict[int, int]
) -> list[dict]:
    """找出「问句里明确写了车系名，但锚点指向**别的**车系」的题——语料自身的矛盾。

    ## 为什么要有这个检查

    `_portable_anchors` 的判据 1 是「id 在本库存在**且其名称出现在问句里**→
    信任 id」。但像「2024款 2.0T 四驱旗舰版」这种**通用款型名跨车系重名**，
    于是语料里那条属于**别的车系**的 id 也「看起来对得上」，被原样信任——
    判据 1 发现不了这类错位。

    实测命中（2026-10-04）：「英菲尼迪QX60 的 2024款 2.0T 四驱旗舰版 和 …」
    问句明写 QX60，锚点却指向「红旗HQ9 PHEV」。

    ## 为什么要紧

    这类题**同时**污染两个方向：
    - 指标侧：`pair_coverage` 等按锚点判定的指标被压低；
    - 修复侧：任何以锚点为准的补召回机制（`_balance_by_series` /
      `_ensure_anchor_coverage`）都会**插入错误车系的切片**并挤掉正确侧——
      那正是它们实测让 pair-coverage 0.439 → 0.4146 → 0.3902 的原因。

    ## 只报告，不改判据

    改锚点重解析口径会让**全部历史基线**失效（README 与 EVAL_LOG 的数字），
    那是人的决定。这里只把它**暴露出来**，让看报告的人先看见问题。
    """
    names = [
        (int(s.id), str(s.name))
        for s in db.scalars(select(VehicleSeries))
        if s.name and len(str(s.name)) >= 2
    ]
    conflicts: list[dict] = []
    for q in questions:
        text = q.get("text") or ""
        anchors = q.get("anchors") or {}
        want: set[int] = set()
        if anchors.get("series_id"):
            want.add(int(anchors["series_id"]))
        for vid in anchors.get("variant_ids") or []:
            sid = variant_series.get(int(vid))
            if sid:
                want.add(int(sid))
        if not want:
            continue
        # 问句里**字面出现**的本库车系名。
        # 只保留**极大**名称：像「岚图 岚图追光 L」「坦克 坦克300L新能源」这种
        # 品牌+车系的拼接，短名是长名的子串，两个都会命中——不去重就会把
        # 「问句其实只提了一个车系」误报成矛盾（实测一度报出 38 条，全是这类）。
        matched = [(sid, name) for sid, name in names if name in text]
        maximal = [
            (sid, name)
            for sid, name in matched
            if not any(name != other and name in other for _, other in matched)
        ]
        in_text = {sid for sid, _ in maximal}
        if not in_text or in_text == want:
            continue
        conflicts.append({
            "id": q.get("id"),
            "text": text,
            "anchor_series": sorted(want),
            "text_series": sorted(in_text),
            "partial": bool(in_text & want),
        })
    return conflicts


def _relevant_ids(q: dict, variant_series: dict[int, int]) -> set[int]:
    """问题的相关车系集合（anchors 即黄金判定）。"""
    anchors = q.get("anchors") or {}
    ids: set[int] = set()
    if anchors.get("series_id"):
        ids.add(int(anchors["series_id"]))
    for vid in anchors.get("variant_ids") or []:
        sid = variant_series.get(int(vid))
        if sid:
            ids.add(sid)
    if anchors.get("variant_id"):
        sid = variant_series.get(int(anchors["variant_id"]))
        if sid:
            ids.add(sid)
    return ids


def _is_relevant(hit: SearchResult, relevant_series: set[int], relevant_variants: set[int]) -> bool:
    if hit.variant_id is not None and hit.variant_id in relevant_variants:
        return True
    return hit.series_id is not None and hit.series_id in relevant_series


def _metrics(ranked_relevance: list[list[bool]], relevant_counts: list[int], k: int) -> dict:
    """从 0/1 相关性序列计算主流指标（ranked_relevance 长度 = EVAL_DEPTH）。"""
    n = len(ranked_relevance)
    if n == 0:
        return {"hit": 0.0, "recall": 0.0, "precision": 0.0, "mrr": 0.0, "ndcg": 0.0}
    hits = recalls = precisions = ndcgs = 0.0
    rrs = 0.0
    for flags, rel_total in zip(ranked_relevance, relevant_counts):
        topk = flags[:k]
        hit = 1.0 if any(topk) else 0.0
        hits += hit
        precisions += sum(topk) / k
        recalls += (sum(topk) / rel_total) if rel_total else 0.0
        rr = 0.0
        for i, f in enumerate(flags):
            if f:
                rr = 1.0 / (i + 1)
                break
        rrs += rr
        dcg = sum(f / math.log2(i + 2) for i, f in enumerate(flags[:k]))
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(rel_total, k))) if rel_total else 0.0
        ndcgs += (dcg / idcg) if idcg else 0.0
    return {
        "hit": round(hits / n, 4),
        "recall": round(recalls / n, 4),
        "precision": round(precisions / n, 4),
        "mrr": round(rrs / n, 4),
        "ndcg": round(ndcgs / n, 4),
    }


# ── 口径修正指标（v2，2026-09）：信息需求对齐判定 ──────────────────────────────
# 旧口径的三处失真（2026-09-11 复盘）：
# 1) semantic/recommend 的「单锚点」判定把一题多解说成不相关 → 约束满足度判定；
# 2) Recall@5 分母 = 相关车系全部切片（均值 15 条，结构上限仅 0.4773）→ 参数题改
#    fact-coverage（实际信息需求是"那条事实"），并把结构上限写入报告供对照；
# 3) compare 需要两侧证据都在场 → pair-coverage。
# 旧指标全部保留并排输出，保证与历史报告可比。


def _build_eval_context(db) -> dict:
    """预载判定所需的车系属性 / 参数事实 / 款型映射（一次性，全部策略共用）。

    车系属性装载走 app/catalog/series_constraints 的共享实现（与流水线 grade 同源）；
    参数事实（含单位，供 fact-coverage 的 needle 构造）为本评测专属。
    """
    param_keys = {key for key, _phrase in _PARAM_KEYS}
    series_attrs = load_series_attrs(db)
    variants = db.scalars(
        select(VehicleVariant).where(VehicleVariant.status == "on_sale")
    ).all()
    facts: dict[int, dict[str, tuple[str, str | None]]] = {}
    for vid, key, value, unit in db.execute(
        select(SpecFact.variant_id, SpecFact.fact_key, SpecFact.fact_value, SpecFact.unit)
        .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
        .where(VehicleVariant.status == "on_sale", SpecFact.fact_key.in_(param_keys))
    ).all():
        if value:
            facts.setdefault(vid, {})[key] = (value, unit)
    variants_by_series: dict[int, list[int]] = {}
    for v in variants:
        variants_by_series.setdefault(v.series_id, []).append(v.id)
    brand_names = {b.id: b.name for b in db.scalars(select(Brand)).all()}
    return {
        "series_attrs": series_attrs,
        "facts": facts,
        "variants_by_series": variants_by_series,
        "brand_names": brand_names,
    }


# _series_satisfies 自 gen_eval_questions 导入（出题与评测共用同一实现，见文件头 import）


def _semantic_constraints(q: dict) -> dict:
    """从 semantic_fuzzy 问题文本反解结构化约束（措辞映射与生成器同源）。

    评审 E16：多处命中且互相冲突时放弃该约束（第一处命中可能来自车系档案自由
    文本而非用户意图——宁可少判不可错判）。
    """
    text = q.get("text") or ""
    out: dict = {}
    energies = {energy for hint, energy in HINT_TO_ENERGY.items() if hint.rstrip("的") in text}
    if len(energies) == 1:
        out["energy_type"] = next(iter(energies))
    bodies = {body for label, body in HEAD_LABEL_TO_BODY.items() if label in text}
    if len(bodies) == 1:
        out["body_type"] = next(iter(bodies))
    return out


def _judge_mode(q: dict, ctx: dict) -> str | None:
    """每题的 v2 判定模式：parameter→fact；compare→pair；未点名车系的推荐/语义→constraint；
    品牌开放题→brand；点名车系的车系问答沿用 series 级旧指标（单答案意图，口径本就正确）。"""
    bucket = q.get("bucket")
    expect = q.get("expect") or {}
    anchors = q.get("anchors") or {}
    sid = anchors.get("series_id")
    text = q.get("text") or ""
    if bucket == "parameter":
        return "fact"
    if bucket == "compare":
        return "pair"
    if bucket == "semantic":
        return "constraint" if _semantic_constraints(q) else None
    if expect.get("brand_name") and not sid:
        return "brand"
    name = ctx["series_attrs"].get(int(sid), {}).get("name") if sid else None
    if sid and name and name in text:
        return None
    if any(k in expect for k in ("budget_max", "energy_type", "body_type", "passengers")):
        return "constraint"
    return None


def _fact_needles(fact_key: str, value: str, unit: str | None) -> list[str]:
    """事实在证据文本中的可能形态：款型切片「键 = 值」与摘要/文本「值 单位」。"""
    needles = [f"{fact_key} = {value}"]
    u = (unit or "").strip()
    if u and not value.strip().endswith(u):
        needles.append(f"{value} {u}")
    return needles


def _fact_coverage(q: dict, hits: list, ctx: dict, variant_series: dict[int, int], k: int) -> float | None:
    """参数题的事实覆盖：相关车系（含锚定款型）的该参数值是否出现在 top-k 证据里。"""
    key = (q.get("expect") or {}).get("fact_key")
    if not key:
        return None
    rel_series = _relevant_ids(q, variant_series)
    needles: list[str] = []
    for sid in rel_series:
        for vid in ctx["variants_by_series"].get(sid, []):
            f = ctx["facts"].get(vid, {}).get(key)
            if f:
                needles.extend(_fact_needles(key, f[0], f[1]))
    if not needles:
        return None  # 数据里没有该参数：覆盖无从谈起（不可回答题另测）
    text = "\n".join(h.text for h in hits[:k])
    return 1.0 if any(n in text for n in needles) else 0.0


def _constraint_metrics(q: dict, hits: list, ctx: dict, k: int) -> dict | None:
    """推荐/语义题：检回车系满足问题约束即相关。返回 valid-hit / valid-precision / valid-MRR。"""
    constraints = _semantic_constraints(q) if q.get("bucket") == "semantic" else (q.get("expect") or {})
    if not any(c in constraints for c in ("budget_max", "budget_min", "energy_type", "body_type", "passengers")):
        return None  # 只有软约束（用途/喜好）的问题不参与判定
    valid = [1.0 if _series_satisfies(ctx["series_attrs"].get(h.series_id), constraints) else 0.0 for h in hits[:k]]
    mrr = 0.0
    for i, v in enumerate(valid):
        if v:
            mrr = 1.0 / (i + 1)
            break
    # 评审 E6：@K 标签随实际 k 参数化（--top-k ≠ 5 时不再错标 @5）
    return {
        f"valid_hit@{k}": 1.0 if any(valid) else 0.0,
        f"valid_precision@{k}": round(sum(valid) / k, 4),
        f"valid_mrr": round(mrr, 4),
    }


def _pair_coverage(q: dict, hits: list, variant_series: dict[int, int], k: int) -> float | None:
    """对比题：两侧锚定车系的证据都必须在场（同车系款型对比不适用，series 级 Hit 已覆盖）。"""
    anchor_vids = (q.get("expect") or {}).get("variant_ids") or (q.get("anchors") or {}).get("variant_ids") or []
    need = {variant_series[int(v)] for v in anchor_vids if variant_series.get(int(v))}
    if len(need) < 2:
        return None
    got = {h.series_id for h in hits[:k] if h.series_id is not None}
    return 1.0 if need <= got else 0.0


def _brand_hit(q: dict, hits: list, ctx: dict, k: int) -> float | None:
    """品牌开放题：检回车系属于锚定品牌即相关（如「比亚迪有哪些在售新能源SUV」）。"""
    brand_name = (q.get("expect") or {}).get("brand_name")
    if not brand_name:
        return None
    want = {
        sid for sid, a in ctx["series_attrs"].items()
        if ctx["brand_names"].get(a.get("brand_id")) == brand_name
    }
    got = {h.series_id for h in hits[:k] if h.series_id is not None}
    return 1.0 if want & got else 0.0


def _v2_metrics_for_question(q: dict, hits: list, ctx: dict, variant_series: dict[int, int], top_k: int) -> dict:
    mode = _judge_mode(q, ctx)
    if mode == "fact":
        cov = _fact_coverage(q, hits, ctx, variant_series, top_k)
        return {} if cov is None else {f"fact_coverage@{top_k}": cov}
    if mode == "constraint":
        return _constraint_metrics(q, hits, ctx, top_k) or {}
    if mode == "pair":
        p = _pair_coverage(q, hits, variant_series, top_k)
        return {} if p is None else {f"pair_coverage@{top_k}": p}
    if mode == "brand":
        b = _brand_hit(q, hits, ctx, top_k)
        return {} if b is None else {f"brand_hit@{top_k}": b}
    return {}


def _mean(values: list) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _run_strategy(
    name: str,
    search_fn,
    questions: list[dict],
    variant_series: dict[int, int],
    chunks_by_series: dict[int, int],
    top_k: int,
    ctx: dict | None = None,
) -> dict:
    ranked_flags: list[list[bool]] = []
    rel_counts: list[int] = []
    # 评审 ⑧：按查询类型分桶累积（parameter/recommend/semantic/compare）
    bucket_flags: dict[str, list[list[bool]]] = {}
    bucket_counts: dict[str, list[int]] = {}
    v2_acc: dict[str, list] = {}
    v2_by_bucket: dict[str, dict[str, list]] = {}
    started = time.perf_counter()
    for q in questions:
        rel_series = _relevant_ids(q, variant_series)
        if not rel_series:
            continue
        rel_variants = set(
            int(v) for v in ((q.get("anchors") or {}).get("variant_ids") or [])
        ) | ({int((q["anchors"])["variant_id"])} if (q.get("anchors") or {}).get("variant_id") else set())
        hits = search_fn(q["text"])
        flags = [_is_relevant(h, rel_series, rel_variants) for h in hits[:EVAL_DEPTH]]
        flags += [False] * (EVAL_DEPTH - len(flags))
        ranked_flags.append(flags)
        bucket = q.get("bucket") or "uncategorized"
        bucket_flags.setdefault(bucket, []).append(flags)
        rel = sum(chunks_by_series.get(sid, 0) for sid in rel_series)
        rel_counts.append(rel)
        bucket_counts.setdefault(bucket, []).append(rel)
        if ctx:
            v2 = _v2_metrics_for_question(q, hits, ctx, variant_series, top_k)
            for key, value in v2.items():
                v2_acc.setdefault(key, []).append(value)
                v2_by_bucket.setdefault(bucket, {}).setdefault(key, []).append(value)
    elapsed = round(time.perf_counter() - started, 1)
    out = {
        "strategy": name,
        "questions": len(ranked_flags),
        "elapsed_seconds": elapsed,
        f"@{top_k}": _metrics(ranked_flags, rel_counts, top_k),
        "@10": _metrics(ranked_flags, rel_counts, 10),
        # 结构上限：Relate@k 的分母是相关车系全部切片，均值 15 条时上限远低于 1.0——
        # 写入报告让 recall 永远能对着上限解读（2026-09-11 口径复盘）
        "recall_ceiling": {
            f"@{top_k}": _mean([min(top_k, rc) / rc for rc in rel_counts if rc]),
            "@10": _mean([min(10, rc) / rc for rc in rel_counts if rc]),
        },
        "v2": {key: _mean(values) for key, values in sorted(v2_acc.items())},
        "v2_questions": {key: len(values) for key, values in sorted(v2_acc.items())},
        "by_bucket": {
            bucket: {
                "questions": len(flags),
                f"@{top_k}": _metrics(flags, bucket_counts[bucket], top_k),
                "@10": _metrics(flags, bucket_counts[bucket], 10),
                "v2": {key: _mean(values) for key, values in sorted(v2_by_bucket.get(bucket, {}).items())},
            }
            for bucket, flags in sorted(bucket_flags.items())
        },
    }
    return out


def _chunks_without_overlap(db) -> list:
    """对照切片：文档正文 overlap=0（其余切片与主流程一致），量化重叠切分收益。

    重叠只在合并阶段给切片加前缀、不改变切分块数，因此可按序号原位替换。
    """
    from dataclasses import replace

    chunks = build_chunks(db)
    docs = {
        d.id: d.content_text or ""
        for d in db.scalars(select(SourceDocument).where(SourceDocument.content_text.isnot(None))).all()
    }
    pieces_by_doc = {doc_id: split_text(text, chunk_overlap=0) for doc_id, text in docs.items()}
    out = []
    for c in chunks:
        if c.kind != "source_document" or c.document_id is None:
            out.append(c)
            continue
        pieces = pieces_by_doc.get(c.document_id)
        idx = int(c.chunk_id.rsplit("#", 1)[-1] or 0)
        if pieces is None or idx >= len(pieces):
            continue
        out.append(replace(c, text=pieces[idx]))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG 检索评测（切分/召回/融合/重排策略对比）")
    parser.add_argument("--questions", default=os.path.join("eval", "questions.json"))
    parser.add_argument("--limit", type=int, default=0, help="只评测前 N 条（0=全部）")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--with-dense", action="store_true", help="评测 dense/hybrid（需 Zilliz 已灌库，产生云端调用）")
    parser.add_argument("--only", default="", help="只评测指定策略（逗号分隔，如 pipeline；默认全部）")
    parser.add_argument("--report", default=os.path.join("eval", "rag_eval_report.json"))
    args = parser.parse_args(argv)
    top_k = min(max(args.top_k, 1), 10)

    if not os.path.exists(args.questions):
        print(f"缺少问题库 {args.questions}：先运行 python tools/gen_eval_questions.py", file=sys.stderr)
        return 1
    payload = json.loads(open(args.questions, encoding="utf-8").read())
    questions = payload.get("questions") or []
    if args.limit:
        questions = questions[: args.limit]

    factory = get_session_factory()
    with factory() as db:
        variant_series = {
            v.id: v.series_id for v in db.scalars(select(VehicleVariant)).all()
        }
        questions, anchor_stats = _portable_anchors(db, questions)
        if anchor_stats["remapped"]:
            print(
                f"锚点已按车系名重解析：{anchor_stats['remapped']} 处（语料 id 与本库行号不一致，"
                f"原 id {anchor_stats['sample_old']}→{anchor_stats['sample_new']}）"
            )
        conflicts = _anchor_text_conflicts(db, questions, variant_series)
        if conflicts:
            print(
                "\n" + "!" * 72 + "\n"
                f"警告：{len(conflicts)} 道题的**问句与锚点自相矛盾**——"
                "问句里明确写了某个车系名，锚点却指向另一个车系。\n"
                "这类题**不是检索的问题**：`pair_coverage` 等按锚点判定的指标会被压低，"
                "而任何以锚点为准的补召回机制都会插入错误车系的切片。\n"
                "样例："
                + "；".join(
                    f"[{c['id']}] {c['text'][:28]}…" for c in conflicts[:3]
                )
                + "\n处置：本工具**只报告不改判据**（改口径会让全部历史基线失效，"
                  "须由人决定）。修复请重跑 tools/gen_eval_questions.py 针对本库生成语料。"
                + "\n" + "!" * 72 + "\n"
            )
        if anchor_stats["stale_ratio"] > 0.5:
            print(
                "\n" + "!" * 72 + "\n"
                f"警告：{anchor_stats['stale_ratio']:.0%} 的锚点在本库找不到对应车系，"
                "**下列指标不可与历史基线比较**。\n"
                "原因：eval/questions.json 的 anchors 写的是**生成时那个数据库的行号**；"
                "换一个库（本地快照 / 另一个环境）行号就对不上了。\n"
                "对策：重跑 tools/gen_eval_questions.py 针对本库重新生成语料，"
                "或使用与语料同源的库。\n" + "!" * 72 + "\n"
            )
        print("构建判定上下文（约束/事实预载）…")
        eval_ctx = _build_eval_context(db)
        print("构建切片（load → chunk）…")
        chunks = build_chunks(db)
        need_noovl = (not args.only.strip()) or "sparse-nooverlap" in {
            s.strip() for s in args.only.split(",")
        }
        chunks_noovl = _chunks_without_overlap(db) if need_noovl else []

    by_kind: dict[str, int] = {}
    for c in chunks:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    chunks_by_series: dict[int, int] = {}
    for c in chunks:
        if c.series_id is not None:
            chunks_by_series[c.series_id] = chunks_by_series.get(c.series_id, 0) + 1
    doc_texts = [c.text for c in chunks if c.kind == "source_document"]
    doc_texts_noovl = [c.text for c in chunks_noovl if c.kind == "source_document"]
    print(f"切片 {len(chunks)} 条：{by_kind}")

    # sparse 检索器（两份切分各建一个 BM25 索引）
    retr = InMemoryRetriever()
    retr.index(chunks)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    need_noovl = (not only) or "sparse-nooverlap" in only
    retr_noovl = None
    if need_noovl:
        retr_noovl = InMemoryRetriever()
        retr_noovl.index(chunks_noovl)

    strategies: list[tuple[str, object]] = []
    if retr_noovl is not None:
        strategies.append(("sparse-nooverlap", lambda text: retr_noovl.search(text, top_k=EVAL_DEPTH)))
    strategies.append(("sparse", lambda text: retr.search(text, top_k=EVAL_DEPTH)))

    dense_retr = None
    if args.with_dense:
        from app.retrieval.zilliz import ZillizRestRetriever

        candidate = ZillizRestRetriever()
        if candidate.available:
            dense_retr = candidate
        else:
            print("--with-dense 指定但 MILVUS_URI/MILVUS_TOKEN 未配置，跳过 dense/hybrid。")
    if dense_retr is not None:
        strategies.append(("dense", lambda text: dense_retr.search(text, top_k=EVAL_DEPTH)))
        strategies.append(
            (
                "hybrid",
                lambda text: rrf_fuse(
                    [retr.search(text, top_k=EVAL_DEPTH), dense_retr.search(text, top_k=EVAL_DEPTH)]
                )[:EVAL_DEPTH],
            )
        )

    # 完整流水线（含实体解析/重排/把关；dense 依 RETRIEVAL_BACKEND 配置）
    from app.rag import service as rag

    rag.reset_index()

    def _pipeline(text: str):
        with factory() as db2:
            return rag.search(db2, text, top_k=EVAL_DEPTH)

    strategies.append(("pipeline", _pipeline))

    # 重排对照：同配置全链路但强制不重排（保持融合序），隔离重排层边际收益。
    # 单次运行同时拿到重排开/关两组指标，避免为对照重跑全策略（dense/hybrid 云端配额）。
    import app.rag.pipeline as _pl

    def _pipeline_norerank(text: str):
        original = _pl.get_reranker
        _pl.get_reranker = lambda: PassThroughReranker()
        try:
            return _pipeline(text)
        finally:
            _pl.get_reranker = original

    strategies.append(("pipeline-norerank", _pipeline_norerank))

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        strategies = [entry for entry in strategies if entry[0] in only]

    results = []
    for name, fn in strategies:
        print(f"评测策略 {name} …")
        try:
            results.append(_run_strategy(name, fn, questions, variant_series, chunks_by_series, top_k, eval_ctx))
        except Exception as err:  # noqa: BLE001 - 单策略失败不中断整体评测
            print(f"  失败：{type(err).__name__}: {str(err)[:200]}")
            results.append({"strategy": name, "error": f"{type(err).__name__}: {str(err)[:200]}"})

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "questions_file": args.questions,
        "top_k": top_k,
        "backend_mode": RETRIEVAL_BACKEND,
        "with_dense": dense_retr is not None,
        "chunking": {
            "config": {"chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP},
            "chunks_total": len(chunks),
            "by_kind": by_kind,
            "source_document_overlap": chunk_stats(doc_texts),
            "source_document_nooverlap": chunk_stats(doc_texts_noovl),
        },
        "strategies": results,
    }
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    md_path = os.path.splitext(args.report)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(_render_md(report))

    # 控制台摘要
    print(f"\n{'策略':<18}{'Hit@K':>8}{'MRR':>8}{'NDCG@10':>9}{'耗时s':>8}")
    for r in results:
        if "error" in r:
            print(f"{r['strategy']:<18}  失败：{r['error'][:60]}")
            continue
        m, m10 = r[f"@{top_k}"], r["@10"]
        print(f"{r['strategy']:<18}{m['hit']:>8}{m['mrr']:>8}{m10['ndcg']:>9}{r['elapsed_seconds']:>8}")
    print(f"\n报告：{args.report}\n      {md_path}")
    return 0


def _render_md(report: dict) -> str:
    lines = [
        "# RAG 检索评测报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 问题库：{report['questions_file']}（真实数据分层抽样，anchors 即相关性判定）",
        f"- 后端模式：{report['backend_mode']}；dense 参与：{report['with_dense']}",
        "",
        "## 切分质量",
        "",
        f"配置：chunk_size={report['chunking']['config']['chunk_size']}，"
        f"overlap={report['chunking']['config']['chunk_overlap']}；"
        f"切片总数 {report['chunking']['chunks_total']}：{report['chunking']['by_kind']}",
        "",
        "| 切分 | 数量 | 均长 | p95 | 超长占比 | 过短占比 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for label, key in (("递归+重叠", "source_document_overlap"), ("无重叠对照", "source_document_nooverlap")):
        s = report["chunking"][key]
        lines.append(
            f"| {label} | {s.get('count', 0)} | {s.get('len_mean', '-')} | {s.get('len_p95', '-')} "
            f"| {s.get('over_size_ratio', '-')} | {s.get('too_short_ratio', '-')} |"
        )
    lines += ["", "## 策略指标", ""]
    k = report["top_k"]
    lines.append(f"| 策略 | Hit@{k} | Recall@{k} | P@{k} | MRR | NDCG@10 | 耗时s |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in report["strategies"]:
        if "error" in r:
            lines.append(f"| {r['strategy']} | 失败 | - | - | - | - | - |")
            continue
        m, m10 = r[f"@{k}"], r["@10"]
        lines.append(
            f"| {r['strategy']} | {m['hit']} | {m['recall']} | {m['precision']} "
            f"| {m['mrr']} | {m10['ndcg']} | {r['elapsed_seconds']} |"
        )
    # 评审 ⑧：按查询类型分桶（parameter/recommend/semantic/compare）
    bucket_names: list[str] = []
    for r in report["strategies"]:
        for b in (r.get("by_bucket") or {}):
            if b not in bucket_names:
                bucket_names.append(b)
    if bucket_names:
        lines += ["", "## 分桶指标（Hit@%d / MRR）" % k, ""]
        header = "| 策略 | " + " | ".join(bucket_names) + " |"
        lines.append(header)
        lines.append("| --- | " + " | ".join(["---"] * len(bucket_names)) + " |")
        for r in report["strategies"]:
            if "error" in r:
                continue
            cells = []
            for b in bucket_names:
                bb = (r.get("by_bucket") or {}).get(b)
                if not bb:
                    cells.append("-")
                else:
                    cells.append(f"{bb[f'@{k}']['hit']} / {bb[f'@{k}']['mrr']}")
            lines.append(f"| {r['strategy']} | " + " | ".join(cells) + " |")
    # 口径修正指标（v2）：信息需求对齐判定（2026-09-11）；@K 标签随 top_k 参数化（评审 E6）
    v2_keys = [
        f"fact_coverage@{k}", f"valid_hit@{k}", f"valid_precision@{k}",
        "valid_mrr", f"pair_coverage@{k}", f"brand_hit@{k}",
    ]
    if any("v2" in r for r in report["strategies"]):
        lines += [
            "",
            "## 口径修正指标（v2：信息需求对齐判定）",
            "",
            "> semantic/recommend 按约束满足度判定（一题多解合法），参数题按事实覆盖判定，",
            "> compare 要求两侧证据在场；与旧口径并排，供历史回归对照。",
            "",
            "| 策略 | " + " | ".join(v2_keys) + " | Recall@5 结构上限 |",
            "| --- | " + " | ".join(["---"] * (len(v2_keys) + 1)) + " |",
        ]
        for r in report["strategies"]:
            if "error" in r or "v2" not in r:
                continue
            v2 = r.get("v2") or {}
            cells = [str(v2.get(key)) if v2.get(key) is not None else "-" for key in v2_keys]
            cells.append(str((r.get("recall_ceiling") or {}).get(f"@{k}")))
            lines.append(f"| {r['strategy']} | " + " | ".join(cells) + " |")
        if bucket_names:
            lines += ["", "### v2 分桶指标", ""]
            header = "| 策略 | " + " | ".join(f"{b} {key}" for b in bucket_names for key in (f"vhit@{k}", f"vprec@{k}")) + " |"
            lines.append(header)
            lines.append("| --- | " + " | ".join(["---"] * (len(bucket_names) * 2)) + " |")
            for r in report["strategies"]:
                if "error" in r or "v2" not in r:
                    continue
                cells = []
                for b in bucket_names:
                    bb = (r.get("by_bucket") or {}).get(b, {}).get("v2") or {}
                    cells.append(str(bb.get(f"valid_hit@{k}") if bb.get(f"valid_hit@{k}") is not None else "-"))
                    cells.append(str(bb.get(f"valid_precision@{k}") if bb.get(f"valid_precision@{k}") is not None else "-"))
                lines.append(f"| {r['strategy']} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "> 判定口径：相关 = 命中切片 series_id/variant_id 与问题 anchors 一致；",
        "> sparse-nooverlap 与 sparse 之差 = 重叠切分收益；hybrid − sparse = RRF 融合收益；",
        "> pipeline − hybrid = 实体解析/重排/把关的端到端收益；分桶用于验收查询路由与各桶弱点。",
        "> v2 口径（2026-09-11）：semantic/recommend 一题多解按约束满足度判定；参数题按事实覆盖；",
        "> compare 两侧证据需在场；Recall@5 的结构上限（相关车系切片数 >> 5）随报告输出。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
