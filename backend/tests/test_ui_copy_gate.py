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


@pytest.mark.parametrize("term", ["SKU", "门户口径", "范围内"])
def test_terms_are_flagged_in_frontend(tmp_path: Path, term: str):
    _write(tmp_path, "web/app/x/page.tsx", f'export default function P() {{ return <p>{term}</p>; }}\n')
    assert scan(tmp_path), f"{term} 应被拦截"
