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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import SourceDocument, VehicleVariant  # noqa: E402
from app.rag.chunking import chunk_stats, split_text  # noqa: E402
from app.rag.ingest import build_chunks  # noqa: E402
from app.rag.rerank import rrf_fuse  # noqa: E402
from app.retrieval.backends import InMemoryRetriever, SearchResult  # noqa: E402
from app.retrieval.config import CHUNK_OVERLAP, CHUNK_SIZE, RETRIEVAL_BACKEND  # noqa: E402

EVAL_DEPTH = 10  # 取回深度：指标 @top_k 与 @10 都从这一份排序列表计算


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


def _run_strategy(
    name: str,
    search_fn,
    questions: list[dict],
    variant_series: dict[int, int],
    chunks_by_series: dict[int, int],
    top_k: int,
) -> dict:
    ranked_flags: list[list[bool]] = []
    rel_counts: list[int] = []
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
        rel_counts.append(sum(chunks_by_series.get(sid, 0) for sid in rel_series))
    elapsed = round(time.perf_counter() - started, 1)
    out = {
        "strategy": name,
        "questions": len(ranked_flags),
        "elapsed_seconds": elapsed,
        f"@{top_k}": _metrics(ranked_flags, rel_counts, top_k),
        "@10": _metrics(ranked_flags, rel_counts, 10),
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
        print("构建切片（load → chunk）…")
        chunks = build_chunks(db)
        chunks_noovl = _chunks_without_overlap(db)

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
    retr_noovl = InMemoryRetriever()
    retr_noovl.index(chunks_noovl)

    strategies: list[tuple[str, object]] = [
        ("sparse-nooverlap", lambda text: retr_noovl.search(text, top_k=EVAL_DEPTH)),
        ("sparse", lambda text: retr.search(text, top_k=EVAL_DEPTH)),
    ]

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

    results = []
    for name, fn in strategies:
        print(f"评测策略 {name} …")
        try:
            results.append(_run_strategy(name, fn, questions, variant_series, chunks_by_series, top_k))
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
    lines += [
        "",
        "> 判定口径：相关 = 命中切片 series_id/variant_id 与问题 anchors 一致；",
        "> sparse-nooverlap 与 sparse 之差 = 重叠切分收益；hybrid − sparse = RRF 融合收益；",
        "> pipeline − hybrid = 实体解析/重排/把关的端到端收益。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
