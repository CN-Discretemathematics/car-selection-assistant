"""详情页「跳转入口」的源码级契约测试（前端没有 TS 测试框架，用最小静态断言兜住表现层）。

为什么需要：
- 优先级（官方优先 / 缺失回退数据来源）已由 `test_api.py::test_vehicle_detail_source_entry`
  在后端守住——那条是真正的口径门禁；
- 但「kind → 标签」这一层只有 TSX 里的三元表达式，`tsc` 与两道静态门禁都发现不了
  「又把入口写回 official_page_url && …」或「两个 kind 用了同一个标签」这类回归，
  渲染级验证脚本又在 gitignored 的 `.tools/` 里、CI 看不到。所以这里退一步：
  直接对详情页源码做契约断言（改动只需同步这几条字符串）。
"""
from __future__ import annotations

from pathlib import Path

DETAIL_PAGE = Path(__file__).resolve().parents[2] / "web" / "app" / "vehicles" / "[series_id]" / "page.tsx"


def _source() -> str:
    return DETAIL_PAGE.read_text(encoding="utf-8")


def test_detail_page_renders_entry_from_external_link():
    """入口必须由后端给的 `external_link` 驱动，不得再自己判断官方链接是否存在。"""
    source = _source()
    assert "detail.external_link" in source, "详情页应从 external_link 渲染跳转入口"
    assert "detail.official_page_url &&" not in source, (
        "详情页不得再自行判断 official_page_url（优先级归后端，否则表现层会绕过口径）"
    )


def test_detail_page_labels_both_kinds():
    """两种 kind 各自的用户可见文案必须都在：官方=官网车型页，来源=数据来源（不冒充官网）。"""
    source = _source()
    assert "查看品牌官网车型页" in source
    assert "查看数据来源" in source
    assert 'kind === "official"' in source, "标签需按 kind 分派"


def test_detail_page_external_link_attributes():
    """外链属性：新窗口 + noopener（PROJECT_PLAN §10 的官方跳转要求）。"""
    source = _source()
    assert 'target="_blank"' in source
    assert 'rel="noopener noreferrer"' in source
