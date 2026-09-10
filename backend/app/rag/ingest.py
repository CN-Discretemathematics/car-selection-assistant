"""摄取流水线（LangGraph）：load → chunk → index。

把数据库中的结构化事实与来源文档构建为检索切片并灌入召回后端：
- load：SQL 装载原料（车系、按款型分层的在售事实、画像聚合数据）；
- chunk：款型级合并切片（一个款型的核心事实合入 1~N 片，metadata 全量携带）+
  车系介绍/摘要切片 + 来源文档正文（递归切分 + 重叠）；
- index：写入目标后端（sparse=进程内 BM25；dense=Zilliz 向量集合）。

事实文本只来自数据库（SourceDocument.content_text / SpecFact / VehicleSeries），
检索结果只作为 Agent 的证据补充，不产生新事实。
节点只返回自身增量；stages/warnings 走 operator.add reducer（见 state.py）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from app.catalog.series_index import HEADLINE_ORDER, display_name, rank_headlines
from app.common.models import (
    Brand,
    MonthlySales,
    OfficialPrice,
    SourceDocument,
    SpecFact,
    VehicleSeries,
    VehicleVariant,
)
from app.rag.chunking import split_text
from app.rag.state import IngestState, make_stage
from app.retrieval.backends import SearchChunk, register_tokens
from app.retrieval.config import CHUNK_SIZE, FACTS_PER_VARIANT, MAX_CHUNKS

# 切片文本噪音：跳过值本身无信息量或纯导购噪声的事实行（评审 RAG-c）
_SKIP_FACT_KEYS = {"优惠信息"}
_SKIP_FACT_VALUES = {"暂无", "-", "--", "无", "未知"}

# 事实进入检索索引的优先级分组（评审 M-M9-1）：座位/尺寸/续航/动力/价格等核心参数
# 优先占用每车系配额；安全/智驾等由后续 SQL 分组兜底；同组按 id 稳定取样。
_PRIORITY_KEY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("座位数(个)", "长*宽*高(mm)", "长度(mm)", "宽度(mm)", "高度(mm)", "轴距(mm)", "级别"),
    (
        "CLTC综合续航(km)", "CLTC纯电续航里程(km)", "WLTC纯电续航里程(km)",
        "WLTC综合油耗(L/100km)", "最低荷电状态油耗(L/100km)WLTC",
        "百公里耗电量(kWh/100km)",
    ),
    (
        "电动机总功率(kW)", "系统综合功率(kW)", "最大功率(kW)", "最大马力(Ps)",
        "官方0-100km/h加速(s)", "电池能量(kWh)", "电池快充时间(小时)", "最高车速(km/h)",
    ),
    (
        "厂商指导价(元)", "能源类型", "驱动形式", "变速箱类型", "发动机", "驱动电机数",
        "前电动机最大功率(kW)", "后电动机最大功率(kW)",
    ),
)

_BODY_LABEL = {"sedan": "轿车", "suv": "SUV", "mpv": "MPV", "pickup": "皮卡"}


def _relax_idle_transaction_timeout(db: Session) -> None:
    """关闭本次构建会话的「事务空闲」超时（RDS 默认 1h）。

    重建的 load/chunk 阶段会长时间只用 Python 处理已取回的行、不再发查询，
    期间只读事务一直挂着；RDS 的 idle_in_transaction_session_timeout 到点会直接
    掐断连接，下一次查询抛 psycopg.errors.IdleInTransactionSessionTimeout
    （2026-09-10 rebuild4 就是这样失败的）。这里只针对构建会话关闭该保护，
    不影响 API 进程；SQLite 等后端没有该参数，跳过。
    """
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        db.execute(text("SET idle_in_transaction_session_timeout = 0"))


def _load(state: IngestState) -> IngestState:
    """装载原料：活跃车系、按车系分层取样的在售事实、画像聚合数据。"""
    started = time.perf_counter()
    db: Session = state["db"]
    limit = state.get("limit") or MAX_CHUNKS
    _relax_idle_transaction_timeout(db)

    series_rows = db.execute(
        select(VehicleSeries, Brand)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(VehicleSeries.active_status == "active")
    ).all()

    # SKU 事实按款型分层取样（优化②——切分改为款型级合并切片，取样粒度同步到款型）；
    # 取样顺序按优先级（核心参数优先，M-M9-1），同优先级按 id 稳定，
    # 每款型截断 FACTS_PER_VARIANT 条。
    #
    # 2026-09-10 重构：去重 + 配额截断全部下推到 SQL 窗口函数。原实现用
    # 「配额 × 过采样倍数」把近百万行（约 6600 款型 × 150 条）四表 ORM 实体拉进内存，
    # 再用 Python 逐行去重——两个后果：① 2C2G 机器被 OOM killer 杀掉构建进程；
    # ② 去重循环期间只读事务空转，被 RDS idle_in_transaction_session_timeout 掐断。
    # 现在 SQL 只回传 ≤ 配额 的行（约 20 万行），语义与原实现一致：
    # 同一款型内 (fact_key, value, unit) 重复时保留优先级最高、id 最小的一条，
    # 再按优先级排序取前 FACTS_PER_VARIANT 条。
    priority_case = case(
        *[
            (SpecFact.fact_key.in_(keys), idx)
            for idx, keys in enumerate(_PRIORITY_KEY_GROUPS)
        ],
        else_=len(_PRIORITY_KEY_GROUPS),
    )
    deduped_facts = (
        select(
            SpecFact.id.label("fid"),
            SpecFact.variant_id.label("vid"),
            priority_case.label("prio"),
            func.row_number()
            .over(
                partition_by=(
                    SpecFact.variant_id,
                    SpecFact.fact_key,
                    func.trim(func.coalesce(SpecFact.fact_value, "")),
                    SpecFact.unit,
                ),
                order_by=(priority_case, SpecFact.id),
            )
            .label("dedup_rn"),
        )
        .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
        .where(VehicleVariant.status == "on_sale")
        .subquery()
    )
    quota_facts = (
        select(
            deduped_facts.c.fid.label("fid"),
            func.row_number()
            .over(
                partition_by=deduped_facts.c.vid,
                order_by=(deduped_facts.c.prio, deduped_facts.c.fid),
            )
            .label("quota_rn"),
        )
        .where(deduped_facts.c.dedup_rn == 1)
        .subquery()
    )
    fact_rows = db.execute(
        select(SpecFact, VehicleVariant, VehicleSeries, Brand)
        .join(quota_facts, SpecFact.id == quota_facts.c.fid)
        .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
        .join(VehicleSeries, VehicleVariant.series_id == VehicleSeries.id)
        .join(Brand, VehicleSeries.brand_id == Brand.id)
        .where(quota_facts.c.quota_rn <= FACTS_PER_VARIANT)
        .order_by(VehicleVariant.series_id, VehicleVariant.id, quota_facts.c.quota_rn)
    ).all()

    # 车系画像聚合原料（摘要切片用）：全量在售事实 + 价格区间 + 在售数 + 最新月销量
    series_ids = [s.id for s, _ in series_rows]
    facts_by_series: dict[int, list[tuple[str, str, str | None, str | None]]] = {}
    if series_ids:
        headline_rows = db.execute(
            select(SpecFact.fact_key, SpecFact.fact_value, SpecFact.unit, SpecFact.cycle, VehicleVariant.series_id)
            .join(VehicleVariant, SpecFact.variant_id == VehicleVariant.id)
            .where(
                VehicleVariant.status == "on_sale",
                VehicleVariant.series_id.in_(series_ids),
            )
        ).all()
        for key, value, unit, cycle, sid in headline_rows:
            facts_by_series.setdefault(sid, []).append((key, value, unit, cycle))

    price_rows = db.execute(
        select(
            VehicleVariant.series_id,
            func.min(OfficialPrice.price_cny),
            func.max(OfficialPrice.price_cny),
        )
        .join(OfficialPrice, OfficialPrice.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.status == "on_sale",
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
        .group_by(VehicleVariant.series_id)
    ).all()

    # 款型当前指导价（款型切片头用；优化②）
    variant_price_rows = db.execute(
        select(OfficialPrice.variant_id, OfficialPrice.price_cny)
        .join(VehicleVariant, OfficialPrice.variant_id == VehicleVariant.id)
        .where(
            VehicleVariant.status == "on_sale",
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
    ).all()

    count_rows = db.execute(
        select(VehicleVariant.series_id, func.count())
        .where(VehicleVariant.status == "on_sale")
        .group_by(VehicleVariant.series_id)
    ).all()

    sales_by_series: dict[int, tuple[str, int, str]] = {}
    latest_month = db.scalar(
        select(func.max(MonthlySales.month)).where(MonthlySales.sales_type.in_(("retail", "portal")))
    )
    if latest_month:
        sales_rows = db.execute(
            select(MonthlySales.series_id, MonthlySales.sales_count, MonthlySales.sales_type)
            .where(
                MonthlySales.month == latest_month,
                MonthlySales.sales_type.in_(("retail", "portal")),
            )
            .order_by(MonthlySales.id)
        ).all()
        for sid, count, sales_type in sales_rows:
            if sid not in sales_by_series or sales_type == "retail":
                sales_by_series[sid] = (latest_month, count, sales_type)

    materials = {
        "series_rows": series_rows,
        "fact_rows": fact_rows,
        "facts_by_series": facts_by_series,
        "price_by_series": {sid: (pmin, pmax) for sid, pmin, pmax in price_rows},
        "price_by_variant": dict(variant_price_rows),
        "count_by_series": dict(count_rows),
        "sales_by_series": sales_by_series,
    }
    return {
        "materials": materials,
        "stages": [make_stage("load", len(series_ids), len(fact_rows), started,
                              {"series": len(series_ids), "facts": len(fact_rows), "doc_quota": limit})],
    }


def _variant_header(
    variant: VehicleVariant, series: VehicleSeries, brand: Brand, price_cny: Any | None
) -> str:
    """款型切片头：品牌 车系 款型名（能源，指导价）——命中即对齐 SKU。"""
    head = f"{brand.name} {series.name}" if not series.name.startswith(brand.name) else series.name
    energy_label = {"BEV": "纯电", "PHEV": "插电混动", "EREV": "增程", "HEV": "油电混动", "ICE": "燃油"}.get(
        variant.energy_type or "", variant.energy_type or ""
    )
    price = ""
    if price_cny:
        price = f"，指导价 {price_cny / 10000:g} 万"
    return f"{head} {variant.display_name}（{energy_label}{price}）核心参数与配置："


def _variant_chunk_text(
    facts: list[SpecFact], header: str, size: int
) -> list[str]:
    """把一个款型的（优先级有序、去重后的）事实合并为若干 ≤size 的切片。

    每行「类别 键 = 值 单位」；头部逐行带上（第一片），后续片重复头部
    保证任意切片独立可读（款型上下文不丢）。
    """
    lines: list[str] = []
    for fact in facts:
        value = (fact.fact_value or "").strip() or "未披露"
        unit = fact.unit
        if unit and value.endswith(unit.strip()):
            unit = None
        if unit and "万" in value and unit in ("元", "万元"):
            unit = None
        line = f"{fact.fact_key} = {value}"
        if unit:
            line += f" {unit}"
        if fact.cycle:
            line += f"（工况 {fact.cycle}）"
        lines.append(line + "。")
    chunks: list[str] = []
    current: list[str] = []
    # 头部只占第一片；后续片重复头部 → 预留长度
    budget = size - len(header)
    for line in lines:
        candidate = "\n".join([*current, line])
        if len(candidate) <= budget or not current:
            current.append(line)
            continue
        chunks.append(header + "\n".join(current))
        current = [line]
    if current:
        chunks.append(header + "\n".join(current))
    return chunks or [header.rstrip("：")]


def _summary_chunk_text(
    series: VehicleSeries,
    brand: Brand,
    headline: dict[str, str],
    price: tuple[Any, Any] | None,
    on_sale_count: int,
    sales: tuple[str, int, str] | None,
) -> str:
    """车系级摘要切片文本（评审 RAG-b）：定位/指导价/核心参数/在售数量/月销量一句话画像。

    事实切片按 SKU 粒度太碎，车系级问题（「XX 怎么样/有什么优点」）应能直接命中最优切片。
    """
    name = display_name(series, brand)
    parts = [f"{name}。"]
    if series.positioning:
        parts.append(f"车型定位：{series.positioning}。")
    if price and price[0] is not None:
        if price[1] is not None and price[1] != price[0]:
            parts.append(f"官方指导价 {price[0] / 10000:g}-{price[1] / 10000:g} 万元。")
        else:
            parts.append(f"官方指导价 {price[0] / 10000:g} 万元。")
    else:
        parts.append("官方指导价：官方资料未披露。")
    parts.append(f"在售 {on_sale_count} 款。")
    head_parts = [headline[label] for label in HEADLINE_ORDER if headline.get(label)]
    if head_parts:
        parts.append("核心参数：" + "；".join(head_parts) + "。")
    if sales:
        month, sales_count, sales_type = sales
        label = "门户口径" if sales_type == "portal" else "零售口径"
        parts.append(f"{month} 月销量 {sales_count:,} 辆（{label}）。")
    return " ".join(parts)


def _chunk(state: IngestState) -> IngestState:
    """构造切片：系列介绍 + SKU 结构化事实 + 车系摘要 + 来源文档正文（递归切分）。"""
    started = time.perf_counter()
    db: Session = state["db"]
    materials = state["materials"]
    limit = state.get("limit") or MAX_CHUNKS
    chunks: list[SearchChunk] = []

    # 优化③：车系/品牌展示名注册为分词专名（查询侧 analyze 也会拼入规范名，
    # 两侧整词一致 → 专名 BM25 精度显著高于碎片切分）
    register_tokens(
        [display_name(s, b) for s, b in materials["series_rows"]]
        + [b.name for _s, b in materials["series_rows"]]
    )

    # 1) 系列介绍（车型定位 + 能源类型，来自 series 表）
    for series, brand in materials["series_rows"]:
        text = f"{brand.name} {series.name}。"
        if series.positioning:
            text += f"车型定位：{series.positioning}。"
        text += f"能源类型：{'/'.join(series.energy_types or [])}。"
        chunks.append(
            SearchChunk(
                chunk_id=f"series-{series.id}",
                text=text,
                kind="series_intro",
                brand_id=series.brand_id,
                series_id=series.id,
                source_id=series.source_id,
                source_url=series.official_page_url,
                last_verified_at=series.last_verified_at,
                extra={"energy_types": series.energy_types or []},
            )
        )

    # 2) 款型级合并切片（优化②）：一个款型的核心事实合入 1~N 个切片，
    #    metadata 带全 series/variant/energy/price/status——检索命中即对齐 SKU，
    #    证据归属映射消失；仅索引在售款型（停售参数不污染证据）。
    facts_by_variant: dict[int, list] = {}
    variant_meta: dict[int, tuple[VehicleVariant, VehicleSeries, Brand]] = {}
    for fact, variant, series, brand in materials["fact_rows"]:
        if fact.fact_key in _SKIP_FACT_KEYS or (fact.fact_value or "").strip() in _SKIP_FACT_VALUES:
            continue
        facts_by_variant.setdefault(variant.id, []).append(fact)
        variant_meta.setdefault(variant.id, (variant, series, brand))
    for vid, facts in facts_by_variant.items():
        variant, series, brand = variant_meta[vid]
        raw_price = materials["price_by_variant"].get(vid)
        # Numeric 列返回 Decimal：extra 会被 JSON 序列化进 Zilliz meta，统一转 float
        price_cny = float(raw_price) if raw_price is not None else None
        header = _variant_header(variant, series, brand, raw_price)
        pieces = _variant_chunk_text(facts, header, CHUNK_SIZE)
        for i, part in enumerate(pieces):
            chunks.append(
                SearchChunk(
                    chunk_id=f"variant-{vid}#{i}",
                    text=part,
                    kind="variant_spec",
                    brand_id=series.brand_id,
                    series_id=series.id,
                    model_year_id=variant.model_year_id,
                    variant_id=vid,
                    source_id=series.source_id,
                    # 取系列官方页便于回链，事实级来源页见 source_documents
                    source_url=series.official_page_url,
                    last_verified_at=max(
                        (f.last_verified_at for f in facts if f.last_verified_at),
                        default=variant.last_verified_at,
                    ),
                    extra={
                        "energy_types": [variant.energy_type],
                        "status": variant.status,
                        "price_cny": price_cny,
                        "body_type": series.body_type,
                    },
                )
            )

    # 3) 车系级摘要切片
    headlines = rank_headlines(materials["facts_by_series"])
    for series, brand in materials["series_rows"]:
        energy = " / ".join(series.energy_types or [])
        body_energy = "、".join(
            [_BODY_LABEL.get(series.body_type or "", series.body_type or ""), energy]
        )
        chunks.append(
            SearchChunk(
                chunk_id=f"summary-{series.id}",
                text=_summary_chunk_text(
                    series,
                    brand,
                    headlines.get(series.id, {}),
                    materials["price_by_series"].get(series.id),
                    materials["count_by_series"].get(series.id, 0),
                    materials["sales_by_series"].get(series.id),
                ),
                kind="series_summary",
                brand_id=series.brand_id,
                series_id=series.id,
                source_id=series.source_id,
                source_url=series.official_page_url,
                last_verified_at=series.last_verified_at,
                extra={"energy_types": series.energy_types or [], "body_energy": body_energy},
            )
        )

    # 4) 来源文档正文（官方车型介绍/配置表/手册等提取文本；只占用剩余配额）
    remaining = max(0, limit - len(chunks))
    doc_rows = db.scalars(
        select(SourceDocument)
        .where(SourceDocument.content_text.isnot(None))
        .order_by(SourceDocument.id)
        .limit(remaining)
    ).all()
    doc_piece_total = 0
    for doc in doc_rows:
        pieces = split_text(doc.content_text or "")
        doc_piece_total += len(pieces)
        for i, part in enumerate(pieces):
            chunks.append(
                SearchChunk(
                    chunk_id=f"doc-{doc.id}#{i}",
                    text=part,
                    kind="source_document",
                    document_id=doc.id,
                    brand_id=doc.brand_id,
                    series_id=doc.series_id,
                    model_year_id=doc.model_year_id,
                    variant_id=doc.variant_id,
                    source_id=doc.source_id,
                    source_url=doc.url,
                    page_or_section=doc.page_or_section,
                    effective_from=doc.effective_from,
                    effective_to=doc.effective_to,
                    # SourceDocument 无 last_verified_at 列，以 updated_at 代表数据新鲜度
                    last_verified_at=doc.updated_at,
                )
            )

    final_chunks = chunks[:limit]
    return {
        "chunks": final_chunks,
        "stages": [make_stage("chunk", doc_piece_total, len(final_chunks), started,
                              {"documents": len(doc_rows), "doc_pieces": doc_piece_total})],
    }


def _index(state: IngestState) -> IngestState:
    """写入目标召回后端（sparse=BM25 / dense=Zilliz；build_chunks 场景 target 为空则跳过）。"""
    started = time.perf_counter()
    target = state.get("target") or ""
    chunks = state.get("chunks") or []
    if not target or not chunks:
        return {}
    from app.rag.service import get_dense_backend, get_sparse_backend

    backend = get_dense_backend() if target == "dense" else get_sparse_backend()
    if backend is None:
        return {
            "stages": [make_stage("index", len(chunks), 0, started, {"target": target, "skipped": True})],
            "warnings": ["dense 后端未配置（MILVUS_URI/MILVUS_TOKEN），跳过索引写入"],
        }
    on_progress: Callable[[int, int], None] | None = state.get("on_progress")
    if target == "dense" and on_progress is not None:
        backend.index(chunks, on_progress=on_progress)  # type: ignore[call-arg]
    else:
        backend.index(chunks)
    return {
        "indexed": len(chunks),
        "stages": [make_stage("index", len(chunks), len(chunks), started, {"target": target})],
    }


def _route_after_chunk(state: IngestState) -> str:
    return "index" if state.get("target") else END


def build_ingest_graph():
    """摄取流水线：START → load → chunk →（有 target 时）index → END。"""
    graph = StateGraph(IngestState)
    graph.add_node("load", _load)
    graph.add_node("chunk", _chunk)
    graph.add_node("index", _index)
    graph.add_edge(START, "load")
    graph.add_edge("load", "chunk")
    graph.add_conditional_edges("chunk", _route_after_chunk, {"index": "index", END: END})
    graph.add_edge("index", END)
    return graph.compile()


_ingest_graph = None


def get_ingest_graph():
    global _ingest_graph
    if _ingest_graph is None:
        _ingest_graph = build_ingest_graph()
    return _ingest_graph


def build_chunks(db: Session, limit: int = MAX_CHUNKS) -> list[SearchChunk]:
    """构建全部检索切片（load + chunk 两节点；不写索引）。"""
    state = get_ingest_graph().invoke({"db": db, "limit": limit, "target": ""})
    return state.get("chunks") or []


def run_ingest(
    db: Session,
    target: str = "sparse",
    limit: int = MAX_CHUNKS,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """执行完整摄取流水线并返回运行摘要（管理后台/索引工具共用）。"""
    state = get_ingest_graph().invoke(
        {"db": db, "limit": limit, "target": target, "on_progress": on_progress}
    )
    chunks = state.get("chunks") or []
    by_kind: dict[str, int] = {}
    for c in chunks:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    return {
        "target": target,
        "chunks": len(chunks),
        "indexed": state.get("indexed", 0),
        "by_kind": by_kind,
        "stages": state.get("stages", []),
        "warnings": state.get("warnings", []),
    }
