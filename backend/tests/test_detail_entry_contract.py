"""详情页「跳转入口」的源码级契约测试（前端没有 TS 测试框架，用最小静态断言兜住表现层）。

为什么需要：
- 优先级（官方优先 / 缺失回退数据来源）已由 `test_api.py::test_vehicle_detail_source_entry`
  在后端守住——那条是真正的口径门禁；
- 但「kind → 标签」这一层只有 TSX 里的三元表达式，`tsc` 与两道静态门禁都发现不了
  「又把入口写回 official_page_url && …」或「两个 kind 用了同一个标签」这类回归，
  渲染级验证脚本又在 gitignored 的 `.tools/` 里、CI 看不到。所以这里退一步：
  直接对详情页源码做契约断言（改动只需同步这几条字符串）。

同一文件里也守住反向口径：**对比页不得出现外部跳转入口**（用户 2026-09-16 的决定：
对比场景不提及官方车型页/来源页链接）。
"""
from __future__ import annotations

from pathlib import Path

WEB_APP = Path(__file__).resolve().parents[2] / "web" / "app"
DETAIL_PAGE = WEB_APP / "vehicles" / "[series_id]" / "page.tsx"
COMPARE_PAGE = WEB_APP / "compare" / "page.tsx"


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
    """外链属性：新窗口 + noopener（对外跳转一律新窗口打开，避免把用户带离本站）。"""
    source = _source()
    assert 'target="_blank"' in source
    assert 'rel="noopener noreferrer"' in source


def test_compare_page_has_no_external_entry():
    """对比页不得出现外部跳转入口（官方车型页 / 数据来源页都不提）。

    用户 2026-09-16 的口径：官方链接数据为空、对比场景不补来源兜底，改为**不提及**这类信息；
    入口只保留在详情页。这条断言防止有人「顺手」把链接加回对比表。
    """
    source = COMPARE_PAGE.read_text(encoding="utf-8")
    for token in ("official_page_url", "external_link", "source_page_url", "官方车型页", "数据来源"):
        assert token not in source, f"对比页不应出现 {token}"


def test_compare_button_jumps_to_analysis_panel():
    """✨按钮 = 跳转到确定性分析面板（一句话结论 + 关键差异，明细可展开）；仅兜底时走 Agent。

    用户 2026-09-16 的口径：点按钮直接跳转，且分析要「先结论后细节」（避免大段文字）。
    """
    source = COMPARE_PAGE.read_text(encoding="utf-8")
    assert 'id="analysis-panel"' in source, "分析面板必须有稳定的跳转锚点"
    assert "scrollIntoView" in source and "dsh:flash-analysis" in source, "应平滑滚动并高亮"
    assert "查看差异分析" in source, "按钮文案应反映「跳转」而非「询问 Agent」"
    # 呈现口径：先结论（verdict/key_points）后明细（默认折叠、可展开）；AI 点评随后台补充
    assert "analysis.verdict" in source and "analysis.key_points" in source
    assert "展开全部" in source and "收起明细" in source
    assert "useAiComment" in source and "AI 点评" in source
    assert "askAgent" in source, "分析不可用时的 Agent 兜底应保留"


def test_agent_surface_has_no_official_links():
    """Agent 侧不得再提供官方/来源跳转（2026-09-16 已删除，防止被顺手加回）。

    判断依据：官方车型页链接无法从现有唯一来源（汽车之家）获得，用户口径是
    「不能轻松收集就删掉，以汽车之家与已入库数据为准」。
    """
    backend_app = Path(__file__).resolve().parents[1] / "app"
    tools_src = (backend_app / "agent" / "tools.py").read_text(encoding="utf-8")
    schemas_src = (backend_app / "agent" / "schemas.py").read_text(encoding="utf-8")
    engine_src = (backend_app / "agent" / "engine.py").read_text(encoding="utf-8")
    chat_src = (WEB_APP / "components" / "AgentChat.tsx").read_text(encoding="utf-8")

    assert "official_link_tool" not in tools_src, "官方车型页链接工具应已删除"
    assert "official_links: list[str]" not in schemas_src, "AgentMessageOut 不应再有 official_links"
    assert "official_page_url: str" not in schemas_src, "RecommendedVariant 不应再有 official_page_url"
    assert "official_links=" not in engine_src, "引擎不应再填充 official_links"
    assert "official_page_url" not in chat_src, "推荐卡片不应再渲染官方车型页链接"
