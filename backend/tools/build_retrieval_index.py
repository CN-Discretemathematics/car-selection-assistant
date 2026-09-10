"""检索索引构建（LangGraph 摄取流水线 load → chunk → index）。

- --target sparse（默认）：重建进程内 BM25 索引（开发验证用，秒级）；
- --target dense：把全量切片（系列介绍 + SKU 事实 + 车系摘要 + 来源文档）经
  embedding 灌入 Zilliz/Milvus 集合（需要 .env 中配置 Milvus 连接与 embedding 服务，
  变量清单见 backend/.env.example）；
- --dry-run：只统计切片，不写索引；--smoke：灌入后做 embedding/检索冒烟。
- dense 构建成功后写构建元数据标记（.tmp/dense-build-meta.json），/ops/rag 状态页
  据此展示集合水位（构建时间/切片数）；销售导入 cron 链式触发增量重建时也复用此逻辑。

用法：
    python tools/build_retrieval_index.py --dry-run
    python tools/build_retrieval_index.py --target sparse
    python tools/build_retrieval_index.py --target dense --limit 20000 --smoke

生产等价入口：管理后台 POST /api/v1/admin/rag/reindex（web /ops/rag 页面可视化操作）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import MonthlySales  # noqa: E402
from app.rag.ingest import build_chunks  # noqa: E402
from app.rag.service import get_dense_backend, run_reindex  # noqa: E402
from app.retrieval.config import MAX_CHUNKS  # noqa: E402


def _latest_sales_month(db) -> str | None:
    """构建时库内最新销量月份（写入 marker 供 /ops/rag 水位对比）。"""
    try:
        from sqlalchemy import func, select

        latest = db.scalar(select(func.max(MonthlySales.month)))
        return str(latest) if latest is not None else None
    except Exception:  # noqa: BLE001 - 查询失败不影响重建主流程
        return None


def write_dense_build_marker(summary: dict, sales_month: str | None = None) -> None:
    """dense 灌入成功后写构建元数据（.tmp 目录随 compose volume 持久化）。"""
    try:
        meta_dir = Path(".tmp")
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "dense-build-meta.json").write_text(
            json.dumps(
                {
                    "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "chunks": summary.get("chunks"),
                    "indexed": summary.get("indexed"),
                    "by_kind": summary.get("by_kind"),
                    "sales_month": sales_month,  # 构建时库内最新销量月份（水位对比直接可用）
                    "warnings": summary.get("warnings", [])[:3],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # 标记文件写失败不阻断（水位显示降级）


def _progress(done: int, total: int) -> None:
    if done % 512 == 0 or done == total:
        print(f"  进度 {done}/{total}（{done * 100 // total}%）", end="\r", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建/灌入检索索引（sparse=BM25 / dense=Zilliz）")
    parser.add_argument("--target", choices=("sparse", "dense"), default="sparse")
    parser.add_argument("--limit", type=int, default=0, help="切片数量上限（0=按 RETRIEVAL_MAX_CHUNKS）")
    parser.add_argument("--dry-run", action="store_true", help="只统计切片，不写入索引")
    parser.add_argument("--smoke", action="store_true", help="dense 灌入后做 embedding/检索冒烟")
    args = parser.parse_args(argv)

    limit = args.limit or None
    with get_session_factory()() as db:
        if args.dry_run:
            chunks = build_chunks(db) if limit is None else build_chunks(db, limit=limit)
            kinds: dict[str, int] = {}
            for c in chunks:
                kinds[c.kind] = kinds.get(c.kind, 0) + 1
            print(f"切片共 {len(chunks)} 条：{kinds}")
            print("dry-run：未写入索引。")
            return 0

        if args.target == "dense":
            dense = get_dense_backend()
            if dense is None:
                print(
                    "错误：dense 后端不可用——RETRIEVAL_BACKEND 需为 milvus 且配置 MILVUS_URI/MILVUS_TOKEN",
                    file=sys.stderr,
                )
                return 1
            print("写入 Zilliz（embedding + 上传，可能耗时数分钟）…")

        summary = run_reindex(db, target=args.target, limit=limit or MAX_CHUNKS,
                              on_progress=_progress if args.target == "dense" else None)
        print()
        print(f"索引构建完成：target={summary['target']} 切片={summary['chunks']} 写入={summary['indexed']}")
        print(f"分布：{summary['by_kind']}")
        for w in summary.get("warnings") or []:
            print(f"警告：{w}")
        if args.target == "dense":
            write_dense_build_marker(summary, sales_month=_latest_sales_month(db))

        if args.smoke and args.target == "dense":
            print("检索冒烟…")
            dense = get_dense_backend()
            assert dense is not None
            try:
                hits = dense.search("家用SUV 大空间 五座", top_k=3)
            except Exception as err:  # noqa: BLE001
                print(f"  失败：{type(err).__name__}: {str(err)[:300]}")
                return 1
            for h in hits:
                print(f"  hit: kind={h.kind} series={h.series_id} score={h.score} text={h.text[:40]}")
            if not hits:
                print("  警告：无命中（集合可能刚灌入尚未建索引，稍后重试即可）")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
