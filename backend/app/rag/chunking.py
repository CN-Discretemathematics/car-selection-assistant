"""切分策略：递归字符切分（RecursiveCharacterTextSplitter 语义）+ 重叠窗口。

主流实践（LangChain / LlamaIndex 同款算法）：
1. 按语义分隔符优先级递归切分：段落 → 换行 → 中文句读 → 英文句读 → 空格 → 字符；
2. 相邻切片保留 chunk_overlap 重叠，避免句界处证据被割裂；
3. 无法再按分隔符切分的超长片段退化为定长硬切（仍带重叠）。

结构化事实（SpecFact）与车系摘要本身即原子切片，不经过本模块；
只有来源文档正文（SourceDocument.content_text）需要切分。
"""
from __future__ import annotations

from app.retrieval.config import CHUNK_OVERLAP, CHUNK_SIZE

# 分隔符优先级：段落 → 行 → 中文句读 → 英文句读 → 空格 → 字符级硬切
DEFAULT_SEPARATORS: tuple[str, ...] = (
    "\n\n", "\n", "。", "！", "？", "；", "…", ". ", "! ", "? ", "; ", " ", "",
)


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """字符级定长切分（带重叠）：分隔符耗尽后的最后手段。"""
    step = max(size - max(overlap, 0), 1)
    pieces = [text[i : i + size] for i in range(0, len(text), step)]
    return [p.strip() for p in pieces if p.strip()]


def _merge_with_overlap(blocks: list[str], overlap: int) -> list[str]:
    """把相邻切片头部拼上上一片尾部（重叠窗口）；拼接后允许超出 chunk_size 至多 overlap。"""
    if overlap <= 0 or len(blocks) < 2:
        return blocks
    out = [blocks[0]]
    for prev, cur in zip(blocks, blocks[1:]):
        tail = prev[-overlap:].strip()
        if tail and not cur.startswith(tail):
            out.append(f"{tail}{cur}")
        else:
            out.append(cur)
    return out


def _split_recursive(text: str, separators: tuple[str, ...], size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    sep = ""
    rest: tuple[str, ...] = ()
    for i, candidate in enumerate(separators):
        if candidate == "":
            break
        if candidate in text:
            sep, rest = candidate, separators[i + 1 :]
            break
    if sep == "":
        return _hard_split(text, size, 0)

    blocks: list[str] = []
    current = ""
    for unit in text.split(sep):
        candidate = unit if not current else current + sep + unit
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            blocks.append(current)
        if len(unit) > size:
            # 单个片段仍超限：用下一级分隔符继续递归
            blocks.extend(_split_recursive(unit, rest, size))
            current = ""
        else:
            current = unit
    if current:
        blocks.append(current)
    return blocks


def split_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    separators: tuple[str, ...] = DEFAULT_SEPARATORS,
) -> list[str]:
    """把长文本切成 ≤chunk_size 的重叠切片（切分失败/空文本返回空列表）。"""
    text = (text or "").strip()
    if not text:
        return []
    if chunk_size <= 0:
        chunk_size = CHUNK_SIZE
    chunk_overlap = max(min(chunk_overlap, chunk_size - 1), 0)
    blocks = [b.strip() for b in _split_recursive(text, separators, chunk_size)]
    blocks = [b for b in blocks if b]
    return _merge_with_overlap(blocks, chunk_overlap)


def chunk_stats(texts: list[str]) -> dict:
    """切分质量报告（评估工具/管理后台用）：长度分布与越界占比。"""
    if not texts:
        return {"count": 0}
    lengths = sorted(len(t) for t in texts)
    n = len(lengths)

    def pct(p: float) -> int:
        return lengths[min(int(n * p), n - 1)]

    return {
        "count": n,
        "len_mean": round(sum(lengths) / n, 1),
        "len_p50": pct(0.50),
        "len_p95": pct(0.95),
        "len_max": lengths[-1],
        # 过短切片（<20 字符）多半是噪声；超过 size+overlap 说明切分策略失效
        "too_short_ratio": round(sum(1 for x in lengths if x < 20) / n, 4),
        "over_size_ratio": round(sum(1 for x in lengths if x > CHUNK_SIZE + CHUNK_OVERLAP) / n, 4),
    }
