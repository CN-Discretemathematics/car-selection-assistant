"""用户可见文案门禁（reviewer/scan_ui_copy.py）测试。

为什么值得测：这条门禁是「内部说明泄漏到用户界面」这类事故的唯一自动化拦截
（2026-09-14 连续两次：详情页脚注整句漏过、后端错误信息与口径标签仍有内部叫法），
而它自身的规则区分很细（术语 vs 措辞、注释/docstring vs 用户文案、标识符 vs 字符串），
规则一松就会静默放行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from reviewer.scan_ui_copy import scan  # noqa: E402


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def test_repo_is_clean():
    """当前仓库必须干净（否则门禁失去意义）。"""
    assert scan(REPO_ROOT) == []


def test_frontend_flags_implementation_notes(tmp_path: Path):
    """详情页那种整句实现说明必须被抓到（这次事故的直接回归）。"""
    _write(tmp_path, "web/app/vehicles/[id]/page.tsx",
           'export default function P() {\n  return <p>缺失数据统一显示「官方资料未披露」，不做猜测补全。</p>;\n}\n')
    hits = scan(tmp_path)
    assert any("不做猜测补全" in h or "猜测补全" in h or "统一显示" in h for h in hits), hits


def test_frontend_skips_comments_and_internal_ops(tmp_path: Path):
    """注释里的技术说明、/ops 运维页与仅它使用的模块不算用户界面。"""
    _write(tmp_path, "web/app/page.tsx", '// 这里用 SKU 表示款型（注释，允许）\nexport default function P() { return null; }\n')
    _write(tmp_path, "web/app/ops/rag/page.tsx", 'export default function P() { return <p>SKU 事实</p>; }\n')
    _write(tmp_path, "web/lib/rag.ts", 'export const L = { spec_fact: "SKU 事实" };\n')
    assert scan(tmp_path) == []


def test_backend_flags_user_visible_strings_only(tmp_path: Path):
    """后端只拦「引号内的中文串」：错误信息/用户文案命中，标识符与 docstring 放过。"""
    _write(tmp_path, "backend/app/comparison/router.py", (
        '"""SKU 对比模块（docstring，允许）。"""\n'
        'SKU_API = "https://example.com/param"\n'          # 标识符 + 无中文 → 放过
        'def f():\n'
        '    # 注释里的 SKU 允许\n'
        '    raise ValueError("SKU 不存在：1")\n'            # 用户可见中文串 → 命中
    ))
    hits = scan(tmp_path)
    assert len(hits) == 1, hits
    assert "SKU 不存在" in hits[0]


def test_backend_flags_caliber_label(tmp_path: Path):
    """后端硬编码的口径标签（会进回复与 RAG 切片）也必须拦。"""
    _write(tmp_path, "backend/app/agent/series_qa.py",
           'def f(t):\n    label = "门户口径" if t == "portal" else "零售口径"\n    return label\n')
    assert any("门户口径" in h for h in scan(tmp_path))


def test_backend_ast_handles_triple_quotes_and_desync(tmp_path: Path):
    """第二轮审查 M3 的回归：行级三引号状态机曾有三类偏差，改用 ast 后都应正确。

    - 源码里出现「三个双引号组成的字符串字面量」时会让状态机失步 → 该文件后续内容被
      整段跳过（静默失守）；
    - 用三引号书写的错误信息曾被整行跳过 → 漏放；
    - 用三个单引号书写的 docstring 曾未被识别 → 误报。
    """
    _write(tmp_path, "backend/app/desync.py", (
        "TRIPLE = '\"\"\"'\n"                       # 旧实现会在这里失步
        "def f():\n"
        "    '''返回 \"SKU 不存在\"（单引号 docstring，允许）'''\n"
        "    raise ValueError('''SKU 不存在''')\n"  # 三引号字符串要拦
    ))
    hits = scan(tmp_path)
    assert any("SKU 不存在" in h for h in hits), f"三引号字符串应被拦截：{hits}"
    assert len(hits) == 1, f"单引号 docstring 不应误报：{hits}"


def test_backend_flags_english_and_fstring_literals(tmp_path: Path):
    """英文文案与 f-string 里的术语也要拦（旧实现只查「引号内中文串」会漏放）。"""
    _write(tmp_path, "backend/app/msg.py",
           'def f(x):\n    raise ValueError("Invalid SKU")\n    return f"{x} 个 SKU"\n')
    assert len(scan(tmp_path)) == 2


@pytest.mark.parametrize("term", ["SKU", "门户口径", "范围内"])
def test_terms_are_flagged_in_frontend(tmp_path: Path, term: str):
    _write(tmp_path, "web/app/x/page.tsx", f'export default function P() {{ return <p>{term}</p>; }}\n')
    assert scan(tmp_path), f"{term} 应被拦截"


# ── footer 文案重复（2026-09 第三次文案事故的回归） ─────────────────────────────

FOOTER_LAYOUT = (
    "export default function L({ children }: { children: React.ReactNode }) {\n"
    "  return (\n"
    "    <body>\n"
    "      {children}\n"
    "      <footer>\n"
    "        <p>\n"
    "          <a href=\"/privacy\">隐私政策</a>\n"
    "          <span>|</span>\n"
    "          数据均带来源与更新时间\n"
    "          <span>|</span>\n"
    "          购车助手内容由 AI 生成，仅供参考\n"
    "        </p>\n"
    "        <p>本站不提供站内交易入口；价格与配置以品牌官网为准。</p>\n"
    "      </footer>\n"
    "    </body>\n"
    "  );\n"
    "}\n"
)


def test_footer_clauses_are_clean_sentences(tmp_path: Path):
    """从 footer 抽出的必须是完整子句（标签/表达式不能粘进子句，否则判不出重复）。"""
    from reviewer.scan_ui_copy import footer_clauses

    _write(tmp_path, "web/app/layout.tsx", FOOTER_LAYOUT)
    clauses = footer_clauses(tmp_path)
    assert "购车助手内容由AI生成" in clauses, clauses
    assert "本站不提供站内交易入口" in clauses, clauses
    assert "价格与配置以品牌官网为准" in clauses, clauses
    assert all("{" not in c and "\x00" not in c for c in clauses), clauses
    assert "隐私政策" not in clauses, "短词不应进入判重集合"


def test_footer_duplicate_in_page_is_flagged(tmp_path: Path):
    """页面正文把 footer 的话再说一遍 → FAIL（首页 AI 标识 / 详情页交易入口声明两处事故）。"""
    _write(tmp_path, "web/app/layout.tsx", FOOTER_LAYOUT)
    _write(tmp_path, "web/app/page.tsx",
           'export default function P() { return <p>购车助手内容由 AI 生成，仅供参考。</p>; }\n')
    _write(tmp_path, "web/app/vehicles/[id]/page.tsx",
           'export default function P() { return <p>本站不提供站内交易入口；价格与配置以品牌官网为准。</p>; }\n')
    hits = scan(tmp_path)
    assert any("购车助手内容由AI生成" in h for h in hits), hits
    assert any("本站不提供站内交易入口" in h for h in hits), hits
    assert any("价格与配置以品牌官网为准" in h for h in hits), hits
    assert all("[footer 重复]" in h for h in hits), hits


def test_footer_duplicate_split_by_tags_is_flagged(tmp_path: Path):
    """同一句被标签切开也要拦（唯一拦截手段是抹平边界后的比对，变异测试显示它曾无覆盖）。"""
    _write(tmp_path, "web/app/layout.tsx", FOOTER_LAYOUT)
    _write(tmp_path, "web/app/x/page.tsx",
           'export default function P() { return <p>购车助手内容由 <b>AI</b> 生成，仅供参考。</p>; }\n')
    hits = scan(tmp_path)
    assert any("购车助手内容由AI生成" in h for h in hits), hits


def test_footer_duplicate_in_lib_constant_is_flagged(tmp_path: Path):
    """文案抽到 lib 常量里被多处复用，同样算重复（检查范围不止 app/page.tsx）。"""
    _write(tmp_path, "web/app/layout.tsx", FOOTER_LAYOUT)
    _write(tmp_path, "web/lib/copy.ts", 'export const NOTE = "数据均带来源与更新时间";\n')
    assert any("数据均带来源与更新时间" in h for h in scan(tmp_path))


def test_footer_comment_mention_is_not_flagged(tmp_path: Path):
    """注释里提到 footer 口径不算用户看到两遍（`//` 行注释与 `{/* */}` 都要跳过）。"""
    _write(tmp_path, "web/app/layout.tsx", FOOTER_LAYOUT)
    _write(tmp_path, "web/app/x/page.tsx",
           "// 免责口径见全局 footer：购车助手内容由 AI 生成，仅供参考\n"
           "{/* 同上：数据均带来源与更新时间 */}\n"
           "export default function P() { return <p>正文</p>; }\n")
    assert scan(tmp_path) == []


def test_footer_short_clause_is_not_flagged(tmp_path: Path):
    """短词（隐私政策/仅供参考）天然会重复，不得误报，否则门禁会被噪声淹没。"""
    _write(tmp_path, "web/app/layout.tsx", FOOTER_LAYOUT)
    _write(tmp_path, "web/app/favorites/page.tsx",
           'export default function P() { return <p>登录后查看收藏（隐私政策）。</p>; }\n')
    assert scan(tmp_path) == []


def test_footer_found_when_refactored_into_component(tmp_path: Path):
    """footer 被拆成子组件时，按 `<footer` 标签自动定位仍然生效（不静默空转）。"""
    _write(tmp_path, "web/app/layout.tsx",
           'export default function L() { return <body><SiteFooter /></body>; }\n')
    _write(tmp_path, "web/app/components/SiteFooter.tsx",
           FOOTER_LAYOUT.replace("export default function L", "export default function SiteFooter"))
    _write(tmp_path, "web/app/page.tsx",
           'export default function P() { return <p>本站不提供站内交易入口；价格与配置以品牌官网为准。</p>; }\n')
    hits = scan(tmp_path)
    assert any("本站不提供站内交易入口" in h for h in hits), hits


def test_footer_duplicate_in_attribute_string_is_flagged(tmp_path: Path):
    """文案写在标签属性里（dangerouslySetInnerHTML / title）同样算重复，不得 fail-open。"""
    _write(tmp_path, "web/app/layout.tsx", FOOTER_LAYOUT)
    _write(tmp_path, "web/app/x/page.tsx",
           "export default function P() {\n"
           "  return <div title=\"购车助手内容由 AI 生成，仅供参考\" dangerouslySetInnerHTML="
           "{{ __html: \"数据均带来源与更新时间\" }} />;\n"
           "}\n")
    hits = scan(tmp_path)
    assert any("购车助手内容由AI生成" in h for h in hits), hits
    assert any("数据均带来源与更新时间" in h for h in hits), hits


def test_footer_multiline_jsx_comment_is_not_flagged(tmp_path: Path):
    """多行 JSX 注释（续行不以 * 开头）也不该被判成正文——块注释要整体剔除。"""
    _write(tmp_path, "web/app/layout.tsx", FOOTER_LAYOUT)
    _write(tmp_path, "web/app/x/page.tsx",
           "{/* 免责口径：\n"
           "    购车助手内容由 AI 生成，仅供参考\n"
           "    数据均带来源与更新时间\n"
           "*/\n"
           "export default function P() { return <p>正文</p>; }\n")
    assert scan(tmp_path) == []


def test_footer_text_after_closing_tag_is_not_a_clause(tmp_path: Path):
    """footer 区间截止到 `</footer>`：其后（如备案信息）不算 footer 文案，不得据此判重。"""
    layout = FOOTER_LAYOUT.replace(
        "      </footer>\n", "      </footer>\n      <p>备案与合规信息由运维统一维护</p>\n"
    )
    _write(tmp_path, "web/app/layout.tsx", layout)
    _write(tmp_path, "web/app/x/page.tsx",
           'export default function P() { return <p>备案与合规信息由运维统一维护</p>; }\n')
    assert scan(tmp_path) == []


def test_footer_body_duplicate_in_layout_is_flagged(tmp_path: Path):
    """footer 文件自己的正文里再写一遍也算重复——只排除 footer 区间，不排除整个文件。"""
    layout = FOOTER_LAYOUT.replace(
        "    <body>\n", "    <body>\n      <p>购车助手内容由 AI 生成，仅供参考</p>\n"
    )
    _write(tmp_path, "web/app/layout.tsx", layout)
    assert any("购车助手内容由AI生成" in h for h in scan(tmp_path))


def test_footer_source_ignores_footer_string_in_ts_file(tmp_path: Path):
    """`const TPL = "<footer>…"` 这种字符串不能当真源，否则会抽出伪子句、误报无关页面。"""
    from reviewer.scan_ui_copy import _footer_source

    _write(tmp_path, "web/app/layout.tsx", 'export default function L() { return <div>无 footer</div>; }\n')
    _write(tmp_path, "web/app/tpl.ts", 'export const TPL = "<footer>本站不提供站内交易入口</footer>";\n')
    rel, why = _footer_source(tmp_path)
    assert rel is None, (rel, why)


def test_footer_check_fails_when_footer_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """定位不到 footer 时不得静默通过：退出码 2，且不能打印「无重复」的 OK 结论。"""
    from reviewer.scan_ui_copy import main

    _write(tmp_path, "web/app/layout.tsx", 'export default function L() { return <div>无 footer</div>; }\n')
    code = main([], root=tmp_path)
    captured = capsys.readouterr()
    assert code == 2, captured
    assert "[FAIL]" in captured.err and "无法判定" in captured.err, captured
    assert "[OK]" not in captured.out, captured


def test_footer_incomplete_structure_reports_clearly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """只有 `<footer` 没有 `</footer>`：要报「结构不完整」，而不是含混的「找不到」。"""
    from reviewer.scan_ui_copy import main

    _write(tmp_path, "web/app/layout.tsx",
           'export default function L() { return <footer><p>本站不提供站内交易入口</p>; }\n')
    code = main([], root=tmp_path)
    captured = capsys.readouterr()
    assert code == 2, captured
    assert "结构不完整" in captured.err, captured
