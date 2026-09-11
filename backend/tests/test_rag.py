"""RAG 模块测试：切分/召回/融合/重排/LangGraph 流水线/摄取。"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.common.models import SourceDocument, SpecFact, VehicleVariant
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


def test_weighted_rrf_fuse():
    """优化⑤：加权 RRF——路权重改变共识相对名次（权重和≠1 也合法）。"""
    from app.rag.rerank import rrf_fuse as fuse

    s1 = SearchResult(chunk_id="only_sparse", score=1.0, text="稀疏独有", kind="variant_spec")
    s2 = SearchResult(chunk_id="both", score=0.9, text="双路共识", kind="variant_spec")
    d1 = SearchResult(chunk_id="both", score=0.9, text="双路共识", kind="variant_spec")
    d2 = SearchResult(chunk_id="only_dense", score=0.8, text="稠密独有", kind="variant_spec")
    # 稀疏加权 0.9：稀疏独有片（0.9/61）仍不及双路共识（0.9/61 + 0.1/62）？——验证单调性
    fused = fuse([[s2, s1], [d1, d2]], k=60, weights=[0.9, 0.1])
    scores = {h.chunk_id: h.score for h in fused}
    assert scores["both"] > scores["only_sparse"] > scores["only_dense"]
    # 等权（None）回退经典 RRF：only_dense 名次贡献 1/61 > only_sparse 1/62
    classic = fuse([[s2, s1], [d1, d2]], k=60)
    assert [h.chunk_id for h in classic] == ["both", "only_dense", "only_sparse"]


def test_expand_query_synonyms():
    """优化⑥：领域同义扩展——追加证据词、不改写原句、空查询幂等。"""
    from app.rag.synonyms import expand_query

    expanded, extra = expand_query("这车省油吗，续航多少")
    assert expanded.startswith("这车省油吗，续航多少")
    assert "馈电油耗" in extra and "纯电续航里程" in extra
    assert expand_query("", max_extra=8) == ("", [])
    # 扩展词有上限
    _, extra2 = expand_query("空间大油耗低的智能家用车")
    assert len(extra2) <= 8


def test_tokenizer_modes(monkeypatch):
    """优化③：分词口径可配置——bigram（默认实测最优）/ jieba 整词 / hybrid。"""
    import app.retrieval.backends as backends

    monkeypatch.setattr(backends, "TOKENIZER", "bigram")
    bigram = backends.tokenize("纯电续航")
    assert "纯电" in bigram and "续航" in bigram and "纯电续航" not in bigram

    monkeypatch.setattr(backends, "TOKENIZER", "hybrid")
    hybrid = backends.tokenize("纯电续航")
    assert "纯电续航" in hybrid and "纯电" in hybrid, "hybrid = 整词 + 二元组"

    monkeypatch.setattr(backends, "TOKENIZER", "jieba")
    word = backends.tokenize("纯电续航")
    assert "纯电续航" in word and "纯电" not in word, "纯整词模式不含二元组"


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
        lambda payload, path: {"results": [{"index": 1, "relevance_score": 0.93}, {"index": 0, "relevance_score": 0.11}]},
    )
    ranked = reranker.rerank("查询", hits, top_k=2)
    assert [h.chunk_id for h in ranked] == ["b", "a"]
    assert ranked[0].score == 0.93
    assert reranker.absolute_scores is True


def test_cross_encoder_rerank_rejects_bad_response(monkeypatch):
    hits = [SearchResult(chunk_id="a", score=0.1, text="文本A", kind="spec_fact")]
    reranker = CrossEncoderReranker(base_url="https://example.com", api_key="k", model="m")
    monkeypatch.setattr(reranker, "_post", lambda payload, path: {"results": [{"index": 99}]})
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
    assert {"series_intro", "variant_spec", "source_document", "series_summary"} <= kinds

    results = rag.search(db_session, "家庭出行 空间", top_k=3)
    assert results, "应命中相关切片"
    assert all(r.text for r in results)
    # 元数据过滤：限定到不存在的 series 无结果
    assert rag.search(db_session, "空间", filters={"series_id": 999999}) == []


def test_build_chunks_samples_per_variant(db_session: Session):
    """优化②：事实按款型取样合并——每个车系都有覆盖，且单款型不超过配额。"""
    from app.retrieval.config import FACTS_PER_VARIANT

    source = make_source(db_session, name="官方测试来源2")
    brand = make_brand(db_session, name="测试品牌2", source=source)
    suv = make_series(db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    sedan = make_series(db_session, brand, name="通勤轿车", body_type="sedan", energy_types=("ICE",), source=source)
    year1 = make_year(db_session, suv)
    year2 = make_year(db_session, sedan)

    def many_facts(n: int) -> list[tuple[str, str, str, str | None, str | None]]:
        return [(f"参数组{i}", f"key{i}", f"值{i}", None, None) for i in range(n)]

    make_variant(db_session, suv, year1, config_version="标准版", energy_type="BEV", facts=many_facts(60), source=source)
    make_variant(db_session, sedan, year2, config_version="标准版", energy_type="ICE", facts=many_facts(60), source=source)
    db_session.commit()

    chunks = build_chunks(db_session)
    variant_chunks = [c for c in chunks if c.kind == "variant_spec"]
    assert variant_chunks, "款型切片必须存在"
    per_series: dict[int, int] = {}
    per_variant_lines: dict[int, int] = {}
    for c in variant_chunks:
        assert c.series_id is not None and c.variant_id is not None
        per_series[c.series_id] = per_series.get(c.series_id, 0) + 1
        # 每行事实带一个「 = 」；头部无
        per_variant_lines[c.variant_id] = per_variant_lines.get(c.variant_id, 0) + c.text.count(" = ")
    assert set(per_series) == {suv.id, sedan.id}, "后段车系必须也有切片进入索引"
    assert max(per_variant_lines.values()) <= FACTS_PER_VARIANT, "单款型事实条数不得超配额"
    assert all(c.extra.get("status") == "on_sale" for c in variant_chunks), "款型切片必须携带 status 元数据"


def test_build_chunks_prioritizes_key_facts(db_session: Session):
    """M-M9-1（优化②沿用）：核心参数（座位数）优先进入每款型配额，即使其 id 排在最后。"""
    from app.retrieval.config import FACTS_PER_VARIANT

    source = make_source(db_session, name="官方测试来源3")
    brand = make_brand(db_session, name="测试品牌3", source=source)
    series = make_series(db_session, brand, name="优先级SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db_session, series)
    # 先塞满配额的低优先级事实，最后才追加一条高优先级「座位数」
    facts = [(f"参数组{i}", f"key{i}", f"value{i}", None, None) for i in range(FACTS_PER_VARIANT)]
    facts.append(("参数信息", "座位数(个)", "5", "个", None))
    make_variant(db_session, series, year, facts=facts, source=source)
    db_session.commit()

    chunks = build_chunks(db_session)
    variant_chunks = [c for c in chunks if c.kind == "variant_spec"]
    assert any("座位数" in c.text for c in variant_chunks), "高优先级事实（座位数）应被取样，即便 id 排在最后"
    total_lines = sum(c.text.count(" = ") for c in variant_chunks)
    assert total_lines == FACTS_PER_VARIANT, "单款型事实条数仍不得超配额"


def test_variant_chunk_text_no_noise(db_session: Session):
    """RAG-c（优化②沿用）：值为「暂无」/优惠信息等噪声行不进入切片，且单位不重复。"""
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
    texts = [c.text for c in build_chunks(db_session) if c.kind == "variant_spec"]
    assert not any("优惠信息" in t for t in texts)
    assert any("电动机总功率(kW) = 150kW。" in t for t in texts), "值自带单位时不得重复拼接"
    assert any("核心参数与配置：" in t for t in texts), "款型切片应带款型头（命中即对齐 SKU）"


def test_variant_chunks_dedupe_within_variant(db_session: Session):
    """优化②去重口径：同款型内同键同值去重为一条；不同款型各自成片（头带各自款型名）。"""
    source = make_source(db_session, name="官方测试来源5")
    brand = make_brand(db_session, name="测试品牌5", source=source)
    suv = make_series(db_session, brand, name="家用SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db_session, suv)
    for cfg in ("标准版", "旗舰版"):
        make_variant(
            db_session, suv, year, config_version=cfg, energy_type="BEV",
            facts=[("参数信息", "轴距(mm)", "3125", "mm", None)], source=source,
        )
    # 同款型重复行：款型内去重
    dup = [v for v in db_session.query(VehicleVariant).all() if v.config_version == "标准版"][0]
    db_session.add(
        SpecFact(variant_id=dup.id, source_id=source.id, category="参数信息",
                 fact_key="轴距(mm)", fact_value="3125", unit="mm")
    )
    db_session.commit()
    chunks = [c for c in build_chunks(db_session) if c.kind == "variant_spec" and c.series_id == suv.id]
    assert len(chunks) == 2, "两个款型各一个（组）切片"
    std = [c for c in chunks if "标准版" in c.text]
    flag = [c for c in chunks if "旗舰版" in c.text]
    assert std and flag, "款型头必须携带款型名（命中即对齐 SKU）"
    for c in std:
        assert c.text.count("轴距(mm) = 3125") == 1, "同款型内重复行应去重为一条"


def test_variant_chunks_dedupe_ignores_surrounding_whitespace(db_session: Session):
    """评审 v5-3：去重口径与 Python strip() 对齐——只有首尾空白差异的值视为同一条。

    旧实现（Python 侧 strip 后比较）会把 "3125" 与 "\\t3125" 当同一条；改成 SQL 窗口
    去重后若只用 ASCII 空格 trim，两者会落进两个分组：白占一个配额槽、切片里还会
    输出两行同样的值。
    """
    source = make_source(db_session, name="官方测试来源6")
    brand = make_brand(db_session, name="测试品牌6", source=source)
    suv = make_series(db_session, brand, name="空白SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db_session, suv)
    variant = make_variant(
        db_session, suv, year, config_version="标准版", energy_type="BEV",
        facts=[("参数信息", "轴距(mm)", "3125", "mm", None)], source=source,
    )
    for value in ("\t3125", "3125 ", "\u30003125"):
        db_session.add(
            SpecFact(variant_id=variant.id, source_id=source.id, category="参数信息",
                     fact_key="轴距(mm)", fact_value=value, unit="mm")
        )
    db_session.commit()

    chunks = [c for c in build_chunks(db_session) if c.kind == "variant_spec" and c.series_id == suv.id]
    lines = sum(c.text.count("轴距(mm) = 3125") for c in chunks)
    assert lines == 1, "仅首尾空白不同的值必须去重为一条，不得白占配额或重复输出"


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


def test_pipeline_parameter_query_skips_dense(db_session: Session, monkeypatch):
    """优化④：参数查询（参数词 + 车系实体）只走稀疏快路——稠密后端根本不被调用。"""
    _seed(db_session)
    rag.reset_index()

    class BoomDense:
        name = "zilliz"
        called = False

        def search(self, *args, **kwargs):
            BoomDense.called = True
            raise RuntimeError("should not be called")

    monkeypatch.setattr(rag, "get_dense_backend", lambda: BoomDense())
    out = rag.try_query(db_session, "家用SUV 的轴距和座位数是多少", top_k=3)
    assert BoomDense.called is False, "参数查询不得触发稠密召回"
    assert not any("dense 召回失败" in w for w in out["run"]["warnings"])
    assert out["results"], "快路仍应返回稀疏结果"
    stage = next(s for s in out["run"]["stages"] if s["node"] == "recall_dense")
    assert stage["detail"].get("skipped") == "parameter_query"
    # 语义查询不受影响：仍会尝试稠密路（这里被 Boom 捕获并降级）
    out2 = rag.try_query(db_session, "空间大的车", top_k=3)
    assert any("dense 召回失败" in w for w in out2["run"]["warnings"])


def test_variant_chunk_extra_json_serializable(db_session: Session):
    """回归：Numeric 列返回 Decimal——chunk.extra 必须能整体 JSON 序列化，
    否则 Zilliz insert（json=dumps）在全部 embedding 完成后才崩溃（实测踩坑）。"""
    import json as _json

    source = make_source(db_session, name="官方测试来源7")
    brand = make_brand(db_session, name="测试品牌7", source=source)
    series = make_series(db_session, brand, name="序列化SUV", body_type="suv", energy_types=("BEV",), source=source)
    year = make_year(db_session, series)
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV", price_cny="129800",
        facts=[("参数信息", "座位数(个)", "5", "个", None)],
        source=source,
    )
    db_session.commit()
    chunks = [c for c in build_chunks(db_session) if c.kind == "variant_spec"]
    assert chunks
    payload = _json.dumps([c.extra for c in chunks])  # 不抛 TypeError 即通过
    assert "129800.0" in payload or "129800" in payload


def test_analyze_query_type_buckets(db_session: Session):
    """优化④：意图分类落桶——parameter/compare/semantic。"""
    _seed(db_session)
    rag.reset_index()
    for text, expected in (
        ("家用SUV 的轴距是多少", "parameter"),
        ("家用SUV 和 通勤轿车 哪个好", "compare"),
        ("适合家用出行", "semantic"),
    ):
        out = rag.try_query(db_session, text, top_k=2)
        assert out["run"]["stages"][0]["detail"].get("query_type") == expected, text


def test_cross_encoder_dashscope_schema(monkeypatch):
    """优化①：DashScope 端点自动适配 /reranks 与原生 text-rerank 双协议。"""
    reranker = CrossEncoderReranker(
        base_url="https://dashscope.aliyuncs.com/api/v1", api_key="k", model="qwen3.7-text-rerank"
    )
    assert reranker._candidate_paths() == ["/reranks", "/services/rerank/text-rerank/text-rerank"]
    # 原生 schema 响应（results 包在 output 下）可解析
    monkeypatch.setattr(
        reranker, "_post",
        lambda payload, path: {"output": {"results": [{"index": 0, "relevance_score": 0.88}]}},
    )
    hits = [SearchResult(chunk_id="a", score=0.1, text="文本A", kind="variant_spec")]
    ranked = reranker.rerank("查询", hits, top_k=1)
    assert ranked[0].score == 0.88
    assert reranker._resolved_path == "/reranks", "首个探测成功的路径被记住复用"


def test_pipeline_rerank_failure_falls_back(db_session: Session, monkeypatch):
    """重排器故障时回退融合序（PassThrough）并记录 warning——实测融合序优于二次词元重排。"""
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
    assert rerank_stage["detail"]["reranker"] == "none"
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


def test_status_snapshot(db_session: Session, monkeypatch):
    import app.rag.rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "RERANK_PROVIDER", "lexical")
    rerank_mod.reset_reranker()
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


def test_reranker_provider_none(db_session: Session, monkeypatch):
    """优化①补测：RERANK_PROVIDER=none → 保持融合序不重排（A/B 实测本语料最优默认）。"""
    import app.rag.rerank as rerank_mod

    monkeypatch.setattr(rerank_mod, "RERANK_PROVIDER", "none")
    rerank_mod.reset_reranker()
    try:
        assert rerank_mod.get_reranker().name == "none"
        hits = [
            SearchResult(chunk_id="a", score=0.5, text="融合第一名", kind="variant_spec"),
            SearchResult(chunk_id="b", score=0.4, text="融合第二名", kind="variant_spec"),
        ]
        ranked = rerank_mod.get_reranker().rerank("查询", hits, top_k=1)
        assert [h.chunk_id for h in ranked] == ["a"], "none 模式保持融合序"
        assert rerank_mod.get_reranker().absolute_scores is False
    finally:
        rerank_mod.reset_reranker()


def test_config_env_float_tolerant(monkeypatch):
    """评审 M-M7-1：float 型检索配置容错——非数字/负数回退默认，合法值生效。"""
    from app.retrieval import config as retrieval_config

    monkeypatch.setenv("RETRIEVAL_EMBED_RETRY_SECONDS", "abc")
    assert retrieval_config._env_float("RETRIEVAL_EMBED_RETRY_SECONDS", 2.0) == 2.0
    monkeypatch.setenv("RETRIEVAL_EMBED_RETRY_SECONDS", "-1")
    assert retrieval_config._env_float("RETRIEVAL_EMBED_RETRY_SECONDS", 2.0) == 2.0
    monkeypatch.setenv("RETRIEVAL_EMBED_RETRY_SECONDS", "3.5")
    assert retrieval_config._env_float("RETRIEVAL_EMBED_RETRY_SECONDS", 2.0) == 3.5


# ── 稠密集合水位（/ops/rag 滞后判定）────────────────────────────────────────
def test_dense_watermark_month_normalization():
    """水位月份归一化：'2026-08' / '202608' / 202608 都归到 YYYYMM 整数。"""
    assert rag._as_yyyymm("2026-08") == 202608
    assert rag._as_yyyymm("202608") == 202608
    assert rag._as_yyyymm(202608) == 202608
    # 评审 v5-10：库里出现未补零的 '2026-8' 也不能退化成无法比较
    assert rag._as_yyyymm("2026-8") == 202608
    assert rag._as_yyyymm(None) is None
    assert rag._as_yyyymm("") is None


def test_dense_watermark_missing_month_is_stale():
    """评审 v5-2：标记有 built_at 但没写销量月份（构建时那次查询失败）不能算「新鲜」。"""
    status = {
        "dense": {"built_at": "2026-09-10T04:00:00+00:00", "sales_month": None},
        "db_counts": {"latest_sales_month": 202608},
    }
    rag.annotate_dense_watermark(status)
    assert status["dense"]["stale"] is True
    assert "销量月份" in status["dense"]["stale_reason"]


def test_dense_watermark_flags_missing_marker():
    """标记缺失（重建从未成功/旧版工具灌入）→ 按滞后暴露，并给出原因。"""
    status = {"dense": {"enabled": True}, "db_counts": {"latest_sales_month": 202608}}
    rag.annotate_dense_watermark(status)
    assert status["dense"]["stale"] is True
    assert status["dense"]["db_sales_month"] == 202608
    assert "dense-build-meta.json" in status["dense"]["stale_reason"]


def test_dense_watermark_detects_lagging_month():
    """集合构建于 2026-07、库内已到 2026-08 → stale=True（2026-09-10 实况）。"""
    status = {
        "dense": {"built_at": "2026-09-08T13:14:00+00:00", "sales_month": "2026-07"},
        "db_counts": {"latest_sales_month": 202608},
    }
    rag.annotate_dense_watermark(status)
    assert status["dense"]["sales_month"] == 202607
    assert status["dense"]["stale"] is True
    assert "202607" in status["dense"]["stale_reason"]


def test_dense_watermark_fresh_when_months_match():
    """月份一致 → stale=False 且无原因文案。"""
    status = {
        "dense": {"built_at": "2026-09-10T20:00:00+00:00", "sales_month": "2026-08"},
        "db_counts": {"latest_sales_month": 202608},
    }
    rag.annotate_dense_watermark(status)
    assert status["dense"]["stale"] is False
    assert status["dense"]["stale_reason"] is None


# ── 评测 v4：约束解析 / compare 双侧均衡 / 约束下推重排 ─────────────────────
def test_parse_constraints():
    from app.rag.pipeline import _parse_constraints

    assert _parse_constraints("预算15万，要纯电SUV，6座以上，有推荐吗") == {
        "budget_max": 150000, "energy_type": "BEV", "body_type": "suv", "passengers": 6,
    }
    assert _parse_constraints("想要增程式的车") == {"energy_type": "EREV"}
    assert _parse_constraints("新能源轿车有哪些") == {"new_energy": True, "body_type": "sedan"}
    assert _parse_constraints("看看车") == {}  # 无约束不触发重排
    # 评审 C2：否定门控与预算方向语义（此前「不要SUV」被判成 SUV、「以上」被判成上限）
    assert _parse_constraints("不要SUV了，看看15万以内的轿车") == {
        "budget_max": 150000, "body_type": "sedan",
    }
    parsed_min = _parse_constraints("20万以上的MPV有哪些")
    assert "budget_max" not in parsed_min, "「以上」是下限，不得产生预算上限"
    assert parsed_min.get("budget_min") == 200000
    assert _parse_constraints("销量30万的轿车有哪些") == {"body_type": "sedan"}, "裸「N万」非预算语境不得误判"
    assert _parse_constraints("10座以上的MPV")["passengers"] == 10, "座位数须取完整数字（旧正则「10座」会取成 0）"
    # 评审 R4#3：预算语境窗口放宽（「预算在/预算大概」+数字 也算）
    assert _parse_constraints("预算在15万左右的轿车")["budget_max"] == 150000
    assert _parse_constraints("预算大概15万的SUV")["budget_max"] == 150000
    # 评审 R4#1：「非常」不是否定（「非常想要SUV」仍解析出 SUV）
    assert _parse_constraints("非常想要SUV")["body_type"] == "suv"
    # 评审 R4#1：对比句不被 4 字窗口跨词吞掉
    assert _parse_constraints("不要轿车要看SUV")["body_type"] == "suv"


def test_balance_by_series_interleaves():
    from app.rag.pipeline import _balance_by_series

    h1a = SearchResult(chunk_id="1a", score=0.9, text="a1", kind="variant_spec", series_id=1)
    h1b = SearchResult(chunk_id="1b", score=0.8, text="a2", kind="variant_spec", series_id=1)
    h2a = SearchResult(chunk_id="2a", score=0.7, text="b1", kind="variant_spec", series_id=2)
    # 单侧挤占：交错后双侧都在前两名
    balanced = _balance_by_series([h1a, h1b, h2a], [1, 2])
    assert [h.chunk_id for h in balanced[:2]] == ["1a", "2a"]
    assert [h.chunk_id for h in balanced] == ["1a", "2a", "1b"]
    # 非对比场景 / 单锚点：原序不动
    assert _balance_by_series([h1a, h1b], [1]) == [h1a, h1b]


def test_ensure_anchor_coverage_adds_missing_side(monkeypatch):
    """评测 v4.1：锚定车系在 top_k 内缺席时按侧补召回（对比题 pair-coverage 的真正根因）。"""
    import app.rag.service as rag_service
    from app.rag.pipeline import _ensure_anchor_coverage

    class FakeBackend:
        def search(self, query, filters=None, top_k=5):
            sid = (filters or {}).get("series_id")
            return [SearchResult(chunk_id=f"c{sid}", score=0.5, text=f"车系{sid}证据",
                                 kind="variant_spec", series_id=sid)]

    monkeypatch.setattr(rag_service, "get_sparse_backend", lambda: FakeBackend())
    state = {"entity_query": "对比 A 和 B", "query": "对比 A 和 B"}
    present = SearchResult(chunk_id="p1", score=0.9, text="A侧证据", kind="variant_spec", series_id=1)
    out, added = _ensure_anchor_coverage(state, [present], [1, 2], 5)
    assert added == 1, "top_k 内缺席的侧数量"
    got = {h.series_id for h in out[:5]}
    assert got == {1, 2}, "缺席侧必须补召回进入 top_k"
    assert out[0].series_id == 2, "补召回的证据插在前部，先于原有结果"


def test_ensure_anchor_coverage_noop_when_both_in_topk(monkeypatch):
    """双侧本就在 top_k 内：零扰动（不触发补召回）。"""
    import app.rag.service as rag_service
    from app.rag.pipeline import _ensure_anchor_coverage

    def _boom(*a, **k):
        raise AssertionError("双侧在场时不应触发补召回")

    monkeypatch.setattr(rag_service, "get_sparse_backend", _boom)
    h1 = SearchResult(chunk_id="1a", score=0.9, text="A侧", kind="variant_spec", series_id=1)
    h2 = SearchResult(chunk_id="2a", score=0.8, text="B侧", kind="variant_spec", series_id=2)
    out, added = _ensure_anchor_coverage({"query": "q"}, [h1, h2], [1, 2], 5)
    assert added == 0 and out == [h1, h2]


def test_reorder_by_constraints_puts_valid_first(monkeypatch):
    import app.catalog.series_constraints as sc
    from app.rag.pipeline import _reorder_by_constraints

    attrs = {
        1: {"name": "纯电SUV", "brand_id": 1, "body_type": "suv", "energy_types": {"BEV"},
            "min_price": 120000.0, "max_seats": 5},
        2: {"name": "燃油轿车", "brand_id": 1, "body_type": "sedan", "energy_types": {"ICE"},
            "min_price": 200000.0, "max_seats": 5},
    }
    monkeypatch.setattr(sc, "load_series_attrs", lambda db, sids=None: attrs)
    invalid = SearchResult(chunk_id="2a", score=0.9, text="燃油车", kind="variant_spec", series_id=2)
    valid = SearchResult(chunk_id="1a", score=0.8, text="纯电车", kind="variant_spec", series_id=1)
    out = _reorder_by_constraints(db=None, results=[invalid, valid],
                                  constraints={"budget_max": 150000, "energy_type": "BEV",
                                               "body_type": "suv"})
    assert [h.chunk_id for h in out] == ["1a", "2a"], "满足约束的证据应排到前面"


def test_compare_query_covers_both_series(db_session: Session):
    """评测 v4 端到端：对比类查询 top-2 必须双侧车系都在场（pair-coverage 修复）。"""
    ids = _seed(db_session)
    rag.reset_index()
    results = rag.search(db_session, "对比 家用SUV标准版 和 通勤轿车舒适版 的配置差异", top_k=5)
    top2 = {r.series_id for r in results[:2]}
    assert top2 == {ids["suv"], ids["sedan"]}, f"双侧证据都应在 top-2：{top2}"


def test_recommend_query_prefers_constraint_satisfying(db_session: Session):
    """评测 v4 端到端：未点名车系 + 硬约束 → 满足约束的车系证据优先。"""
    ids = _seed(db_session)
    rag.reset_index()
    results = rag.search(db_session, "预算15万，要纯电SUV，5座以上，有推荐吗", top_k=3)
    assert results, "应命中证据"
    assert results[0].series_id == ids["suv"], "燃油轿车证据不得排在纯电SUV之前（约束满足度优先）"

