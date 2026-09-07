"""RAG 模块测试：切分/召回/融合/重排/LangGraph 流水线/摄取。"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.common.models import SourceDocument
from app.rag import service as rag
from app.rag.chunking import chunk_stats, split_text
from app.rag.ingest import build_chunks
from app.rag.rerank import CrossEncoderReranker, LexicalReranker, rrf_fuse
from app.retrieval.backends import InMemoryRetriever, SearchChunk, SearchResult, tokenize
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> dict[str, int]:
    """两车系 + 事实 + 来源文档（流水线端到端用例共用）。"""
    source = make_source(db, name="官方测试来源")
    brand = make_brand(db, name="测试品牌", source=source)
    suv = make_series(db, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    sedan = make_series(db, brand, name="通勤轿车", body_type="sedan", energy_types=("ICE",), source=source)
    y1, y2 = make_year(db, suv), make_year(db, sedan)
    make_variant(
        db, suv, y1, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[("尺寸", "长*宽*高(mm)", "4820*1900*1700", "mm", None), ("座位数", "座位数(个)", "5", "座", None)],
        source=source,
    )
    make_variant(
        db, sedan, y2, config_version="舒适版", energy_type="ICE", price_cny="99800",
        facts=[("动力", "电动机总功率(kW)", "115", "kW", None)], source=source,
    )
    db.add(
        SourceDocument(
            series_id=suv.id, source_id=source.id, url="https://example.com/spec.pdf",
            source_type="official_doc",
            content_text="第一段：大五座家用SUV，空间充裕，适合家庭出行。\n\n第二段：搭载智能座舱与驾驶辅助。",
            effective_from=date(2025, 1, 1),
        )
    )
    db.commit()
    return {"suv": suv.id, "sedan": sedan.id, "source": source.id}


# ── 切分（chunking）──────────────────────────────────────────────────────────
def test_split_text_size_and_separators():
    text = "第一段讲空间。第二段讲动力。\n\n第三段讲智能化配置与驾驶辅助能力。"
    chunks = split_text(text, chunk_size=14, chunk_overlap=0)
    assert chunks and all(len(c) <= 14 for c in chunks)
    # 语义分隔符优先：按段落/句子边界装填，不从句中截断
    assert chunks[0] == "第一段讲空间。第二段讲动力。"


def test_split_text_overlap_prefix():
    text = "aaaa。bbbb。cccc。dddd。"
    chunks = split_text(text, chunk_size=9, chunk_overlap=4)
    assert len(chunks) >= 2
    # 后一片头部携带前一片尾部（重叠窗口）
    assert chunks[1].startswith(chunks[0][-4:].strip()) or chunks[0][-4:].strip() in chunks[1]


def test_split_text_hard_split_and_empty():
    long_text = "字" * 1200  # 无任何分隔符 → 字符级硬切
    chunks = split_text(long_text, chunk_size=500, chunk_overlap=50)
    assert len(chunks) >= 3
    assert split_text("", chunk_size=100) == []
    assert split_text("   \n  ", chunk_size=100) == []


def test_chunk_stats():
    stats = chunk_stats(["短", "x" * 30, "y" * 60])
    assert stats["count"] == 3
    assert stats["too_short_ratio"] > 0
    assert chunk_stats([]) == {"count": 0}


# ── 召回后端（BM25）──────────────────────────────────────────────────────────
def test_cjk_segmentation_and_words():
    tokens = tokenize("150 kW 功率 家用SUV")
    assert "150" in tokens and "kw" in tokens
    assert any(t in tokens for t in ("家用", "用s", "su", "uv"))


def test_inmemory_retriever_rank_and_filter():
    retriever = InMemoryRetriever()
    retriever.index(
        [
            SearchChunk(chunk_id="a", text="家用SUV 大空间 五座 适合家庭出行", kind="series_intro", series_id=1, extra={"energy_types": ["BEV"]}),
            SearchChunk(chunk_id="b", text="通勤家轿 低油耗 适合上下班通勤", kind="series_intro", series_id=2, extra={"energy_types": ["ICE"]}),
        ]
    )
    results = retriever.search("家庭出行 空间")
    assert results[0].chunk_id == "a"

    filtered = retriever.search("适合", filters={"series_id": 2}, top_k=5)
    assert all(r.series_id == 2 for r in filtered)

    # 能源过滤：列表与标量统一按「包含」判断
    by_energy = retriever.search("适合", filters={"energy_type": "ICE"}, top_k=5)
    assert [r.chunk_id for r in by_energy] == ["b"]
    assert retriever.search("适合", filters={"energy_type": "PHEV"}, top_k=5) == []
    assert retriever.search("", top_k=5) == []
    # 状态统计访问器（管理后台）
    assert len(retriever.chunks) == 2


# ── 融合与重排 ────────────────────────────────────────────────────────────────
def test_rrf_fuse_consensus_and_dedupe():
    a = SearchResult(chunk_id="a", score=0.9, text="共同命中", kind="series_intro")
    b = SearchResult(chunk_id="b", score=0.8, text="仅稀疏", kind="spec_fact")
    c = SearchResult(chunk_id="c", score=0.7, text="仅稠密", kind="spec_fact")
    a2 = SearchResult(chunk_id="a", score=0.5, text="共同命中", kind="series_intro")
    # 旧 dense 集合缺 meta.chunk_id：同文本、不同 id（z<md5> 回退）也须去重
    a3 = SearchResult(chunk_id="z9f8e7d6c5b4a321", score=0.4, text="共同命中", kind="series_intro")
    fused = rrf_fuse([[a, b], [a2, c, a3]], k=60)
    # 双路共识的 a 排第一；b/c 单路各居其后；a 按 id 与文本两级去重只出现一次
    assert [h.chunk_id for h in fused] == ["a", "b", "c"]
    assert fused[0].score == round(1 / 61 + 1 / 61, 6)


def test_lexical_rerank_entity_boost():
    hits = [
        SearchResult(chunk_id="a", score=0.60, text="腾势 腾势Z9GT 动力 850 kW", kind="spec_fact", series_id=1),
        SearchResult(chunk_id="b", score=0.70, text="欧拉 好猫 动力 105 kW", kind="spec_fact", series_id=2),
    ]
    ranked = LexicalReranker().rerank("腾势Z9GT", hits, top_k=2)
    assert ranked[0].chunk_id == "a", "与查询共享词元的切片应排到前面（即使召回分略低）"
    assert LexicalReranker().rerank("腾势Z9GT", [], top_k=2) == []


def test_cross_encoder_rerank_parses_response(monkeypatch):
    hits = [
        SearchResult(chunk_id="a", score=0.1, text="文本A", kind="spec_fact"),
        SearchResult(chunk_id="b", score=0.2, text="文本B", kind="spec_fact"),
    ]
    reranker = CrossEncoderReranker(base_url="https://example.com", api_key="k", model="m")
    monkeypatch.setattr(
        reranker, "_post",
        lambda payload: {"results": [{"index": 1, "relevance_score": 0.93}, {"index": 0, "relevance_score": 0.11}]},
    )
    ranked = reranker.rerank("查询", hits, top_k=2)
    assert [h.chunk_id for h in ranked] == ["b", "a"]
    assert ranked[0].score == 0.93
    assert reranker.absolute_scores is True


def test_cross_encoder_rerank_rejects_bad_response(monkeypatch):
    hits = [SearchResult(chunk_id="a", score=0.1, text="文本A", kind="spec_fact")]
    reranker = CrossEncoderReranker(base_url="https://example.com", api_key="k", model="m")
    monkeypatch.setattr(reranker, "_post", lambda payload: {"results": [{"index": 99}]})
    try:
        reranker.rerank("查询", hits, top_k=1)
        raise AssertionError("越界 index 应视为无效响应")
    except RuntimeError:
        pass


# ── 摄取（build_chunks）──────────────────────────────────────────────────────
def test_build_chunks_kinds_and_search_flow(db_session: Session):
    rag.reset_index()
    _seed(db_session)

    chunks = build_chunks(db_session)
    kinds = {c.kind for c in chunks}
    assert {"series_intro", "spec_fact", "source_document", "series_summary"} <= kinds

    results = rag.search(db_session, "家庭出行 空间", top_k=3)
    assert results, "应命中相关切片"
    assert all(r.text for r in results)
    # 元数据过滤：限定到不存在的 series 无结果
    assert rag.search(db_session, "空间", filters={"series_id": 999999}) == []


def test_build_chunks_samples_per_series(db_session: Session):
    """评审 M9-2：事实切片按车系分层取样——每个车系都有覆盖，且单车系不超过配额。"""
    from app.retrieval.config import FACTS_PER_SERIES

    source = make_source(db_session, name="官方测试来源2")
    brand = make_brand(db_session, name="测试品牌2", source=source)
    suv = make_series(db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    sedan = make_series(db_session, brand, name="通勤轿车", body_type="sedan", energy_types=("ICE",), source=source)
    year1 = make_year(db_session, suv)
    year2 = make_year(db_session, sedan)

    def many_facts(n: int) -> list[tuple[str, str, str, str | None, str | None]]:
        return [(f"参数组{i}", f"key{i}", f"值{i}", None, None) for i in range(n)]

    make_variant(db_session, suv, year1, config_version="标准版", energy_type="BEV", facts=many_facts(40), source=source)
    make_variant(db_session, sedan, year2, config_version="标准版", energy_type="ICE", facts=many_facts(40), source=source)
    db_session.commit()

    chunks = build_chunks(db_session)
    fact_chunks = [c for c in chunks if c.kind == "spec_fact"]
    assert len(fact_chunks) <= 2 * FACTS_PER_SERIES
    per_series: dict[int, int] = {}
    for c in fact_chunks:
        assert c.series_id is not None
        per_series[c.series_id] = per_series.get(c.series_id, 0) + 1
    assert set(per_series) == {suv.id, sedan.id}, "后段车系必须也有切片进入索引"
    assert max(per_series.values()) <= FACTS_PER_SERIES


def test_build_chunks_prioritizes_key_facts(db_session: Session):
    """评审 M-M9-1：核心参数（座位数）优先进入每车系配额，即使其 id 排在最后。"""
    from app.retrieval.config import FACTS_PER_SERIES

    source = make_source(db_session, name="官方测试来源3")
    brand = make_brand(db_session, name="测试品牌3", source=source)
    series = make_series(db_session, brand, name="优先级SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db_session, series)
    # 先塞满配额的低优先级事实，最后才追加一条高优先级「座位数」
    facts = [(f"参数组{i}", f"key{i}", f"value{i}", None, None) for i in range(FACTS_PER_SERIES)]
    facts.append(("参数信息", "座位数(个)", "5", "个", None))
    make_variant(db_session, series, year, facts=facts, source=source)
    db_session.commit()

    chunks = build_chunks(db_session)
    fact_chunks = [c for c in chunks if c.kind == "spec_fact"]
    assert any("座位数" in c.text for c in fact_chunks), "高优先级事实（座位数）应被取样，即便 id 排在最后"
    assert len(fact_chunks) == FACTS_PER_SERIES, "单车系事实切片仍不得超配额"


def test_fact_chunk_text_no_noise(db_session: Session):
    """评审 RAG-c：值为「暂无」/优惠信息等噪声行不产生切片，且单位不重复。"""
    source = make_source(db_session, name="官方测试来源4")
    brand = make_brand(db_session, name="测试品牌4", source=source)
    suv = make_series(db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db_session, suv)
    make_variant(
        db_session, suv, year, config_version="标准版", energy_type="BEV",
        facts=[
            ("门店报价", "优惠信息", "暂无", None, None),
            ("参数信息", "电动机总功率(kW)", "150kW", "kW", None),
        ],
        source=source,
    )
    db_session.commit()
    texts = [c.text for c in build_chunks(db_session) if c.kind == "spec_fact"]
    assert not any("优惠信息" in t for t in texts)
    assert any("电动机总功率(kW) = 150kW。" in t for t in texts), "值自带单位时不得重复拼接"


def test_fact_chunks_dedupe_cross_variant(db_session: Session):
    """评审 M-R10：同键同值跨款重复 → 去重为一条车系级切片（不带款型名）；
    同键多值 → 每款一条且保留款型名（参数差异不被误合并）。"""
    source = make_source(db_session, name="官方测试来源5")
    brand = make_brand(db_session, name="测试品牌5", source=source)
    suv = make_series(db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db_session, suv)
    for cfg in ("标准版", "旗舰版"):
        make_variant(
            db_session, suv, year, config_version=cfg, energy_type="BEV",
            facts=[("参数信息", "轴距(mm)", "3125", "mm", None)], source=source,
        )
    db_session.commit()
    chunks = [c for c in build_chunks(db_session) if c.kind == "spec_fact" and c.series_id == suv.id]
    assert len(chunks) == 1, "同键同值跨款重复应去重为一条"
    assert "轴距(mm)" in chunks[0].text
    assert "标准版" not in chunks[0].text and "旗舰版" not in chunks[0].text, "全系统一值不应带款型名"

    source2 = make_source(db_session, name="官方测试来源6")
    brand2 = make_brand(db_session, name="测试品牌6", source=source2)
    sedan = make_series(db_session, brand2, name="通勤轿车", body_type="sedan", energy_types=("BEV",), source=source2)
    year2 = make_year(db_session, sedan)
    make_variant(
        db_session, sedan, year2, config_version="标准版", energy_type="BEV",
        facts=[("参数信息", "CLTC纯电续航里程(km)", "500", "km", "CLTC")], source=source2,
    )
    make_variant(
        db_session, sedan, year2, config_version="旗舰版", energy_type="BEV",
        facts=[("参数信息", "CLTC纯电续航里程(km)", "650", "km", "CLTC")], source=source2,
    )
    db_session.commit()
    chunks2 = [c for c in build_chunks(db_session) if c.kind == "spec_fact" and c.series_id == sedan.id]
    assert len(chunks2) == 2, "同键多值不应误合并"
    assert any("标准版" in c.text for c in chunks2) and any("旗舰版" in c.text for c in chunks2)


# ── 查询流水线（LangGraph）────────────────────────────────────────────────────
def test_pipeline_stages_and_entity_filter(db_session: Session):
    ids = _seed(db_session)
    rag.reset_index()

    out = rag.try_query(db_session, "家用SUV怎么样", top_k=3)
    nodes = [s["node"] for s in out["run"]["stages"]]
    assert nodes[0] == "analyze" and nodes[-3:] == ["fuse", "rerank", "grade"]
    assert sorted(nodes[1:3]) == ["recall_dense", "recall_sparse"]  # 并行分支完成顺序不定
    assert out["run"]["resolved_series"], "应解析出车系实体"
    assert out["results"] and all(r["series_id"] == ids["suv"] for r in out["results"] if r["series_id"])

    # 运行日志记录（管理后台可视化数据源）
    runs = rag.recent_runs()
    assert runs and runs[0]["query"] == "家用SUV怎么样"


def test_pipeline_empty_query_short_circuits(db_session: Session):
    _seed(db_session)
    rag.reset_index()
    out = rag.try_query(db_session, "", top_k=3)
    assert out["results"] == []
    assert [s["node"] for s in out["run"]["stages"]] == ["analyze"], "空查询应在 analyze 后直接 END"


def test_pipeline_dense_failure_degrades(db_session: Session, monkeypatch):
    """dense 召回异常时降级为纯稀疏并记录 warning（原则 7）。"""
    _seed(db_session)
    rag.reset_index()

    class BoomDense:
        name = "zilliz"

        def search(self, *args, **kwargs):
            raise RuntimeError("cloud down")

    monkeypatch.setattr(rag, "get_dense_backend", lambda: BoomDense())
    out = rag.try_query(db_session, "家庭出行 空间", top_k=3)
    assert any("dense 召回失败" in w for w in out["run"]["warnings"])
    assert out["results"], "降级后仍应有稀疏结果"


def test_pipeline_rerank_failure_falls_back(db_session: Session, monkeypatch):
    """重排器故障时回退 lexical 并记录 warning。"""
    import app.rag.pipeline as pipeline

    _seed(db_session)
    rag.reset_index()

    class BoomReranker:
        name = "boom"

        def rerank(self, query, results, top_k):
            raise RuntimeError("rerank down")

    monkeypatch.setattr(pipeline, "get_reranker", lambda: BoomReranker())
    out = rag.try_query(db_session, "家庭出行", top_k=3)
    assert any("重排失败" in w for w in out["run"]["warnings"])
    rerank_stage = next(s for s in out["run"]["stages"] if s["node"] == "rerank")
    assert rerank_stage["detail"]["reranker"] == "lexical"
    assert out["results"]


def test_graph_spec_visualization():
    spec = rag.graph_spec()
    assert {"query", "ingest"} <= spec.keys()
    query_nodes = {n["id"] for n in spec["query"]["nodes"]}
    assert {"analyze", "recall_sparse", "recall_dense", "fuse", "rerank", "grade"} <= query_nodes
    assert any(e["conditional"] for e in spec["query"]["edges"]), "analyze 后应有条件边"
    assert "flowchart" in spec["query"]["mermaid"] and "flowchart" in spec["ingest"]["mermaid"]
    ingest_nodes = {n["id"] for n in spec["ingest"]["nodes"]}
    assert {"load", "chunk", "index"} <= ingest_nodes


def test_status_snapshot(db_session: Session):
    _seed(db_session)
    rag.reset_index()
    rag.search(db_session, "家用SUV", top_k=1)
    status = rag.get_status(db_session)
    assert status["backend_mode"] == "inmemory"
    assert status["sparse"]["chunks"] > 0
    assert status["sparse"]["by_kind"]["series_intro"] == 2
    assert status["dense"]["enabled"] is False
    assert status["reranker"]["active"] == "lexical"
    assert status["db_counts"]["series"] == 2


def test_config_env_float_tolerant(monkeypatch):
    """评审 M-M7-1：float 型检索配置容错——非数字/负数回退默认，合法值生效。"""
    from app.retrieval import config as retrieval_config

    monkeypatch.setenv("RETRIEVAL_EMBED_RETRY_SECONDS", "abc")
    assert retrieval_config._env_float("RETRIEVAL_EMBED_RETRY_SECONDS", 2.0) == 2.0
    monkeypatch.setenv("RETRIEVAL_EMBED_RETRY_SECONDS", "-1")
    assert retrieval_config._env_float("RETRIEVAL_EMBED_RETRY_SECONDS", 2.0) == 2.0
    monkeypatch.setenv("RETRIEVAL_EMBED_RETRY_SECONDS", "3.5")
    assert retrieval_config._env_float("RETRIEVAL_EMBED_RETRY_SECONDS", 2.0) == 3.5
