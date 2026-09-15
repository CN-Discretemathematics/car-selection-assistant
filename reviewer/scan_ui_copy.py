"""用户界面/接口文案门禁：公开页面与后端返回文案不得出现内部术语与实现说明，
且全局 footer 的免责/合规文案不得在页面正文里再重复一遍。

为什么需要（2026-09 事故，三次）：
1. 详情页脚注曾写着「缺失数据统一显示「官方资料未披露」，不做猜测补全。」——这是写给我们
   自己看的实现说明；第一次清理只查了固定术语表（SKU/§…），这句话没有术语词，整句漏过，
   直到用户再次反馈才发现。
2. 后端：对比页直接展示的 API 错误信息里仍有「SKU 不存在」（`comparison/router.py`）。
3. 页面底部把全局 footer 的话又说了一遍：首页重复「隐私政策 · 购车助手内容由 AI 生成，
   仅供参考。」，详情页重复「本站不提供站内交易入口…价格与配置以品牌官网为准。」——
   同样是用户看到两遍才发现（`layout.tsx` 的 footer 已经全站覆盖）。

检查范围与口径：
- 前端：`web/app`、`web/lib`（排除 `/ops` 运维页与仅它使用的 `web/lib/rag.ts`）；
  `.tsx/.ts` 一律扫描，整行注释跳过。
- 后端：`backend/app/**/*.py` 只扫**用户可见字符串**——注释与 docstring 里的 SKU 等
  技术术语是给维护者的，允许保留；故用 `ast` 取字符串字面量，再按 docstring 语义排除。
- footer 重复：以全局 footer 为唯一真源（默认 `web/app/layout.tsx`，实际按 `<footer` 标签
  自动定位，拆成组件也能找到），把它切成子句后回查其余前端文件，逐字重复即判 FAIL；
  只比对**长度 ≥ FOOTER_MIN_CLAUSE 的子句**（「隐私政策」这类短词天然会重复，不算事故）。
  判的是「同一句话出现两遍」，不做近义改写判断。定位不到唯一 footer 时**退出码 2**
  （无法判定 ≠ 通过），避免重构后门禁静默空转。

用法（仓库根目录）：

    python reviewer/scan_ui_copy.py            # 干净则退出码 0；1=文案问题；2=footer 无法定位
    python reviewer/scan_ui_copy.py --verbose  # 逐条打印命中位置

与 `reviewer/scan_secrets.py` 一样属于「提交前必过」的静态门禁。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

# ── 前端 ────────────────────────────────────────────────────────────────────
UI_ROOTS = ("web/app", "web/lib")
# 内部页面专用模块：web/lib/rag.ts 只被 /ops/rag 引用，其中「SKU 事实」等标签属运维页
INTERNAL_FILES = ("web/lib/rag.ts",)

# ── 后端 ────────────────────────────────────────────────────────────────────
BACKEND_ROOT = "backend/app"
BACKEND_JARGON = ("SKU", "§", "门户口径", "幂等", "落库")
BACKEND_PHRASES = ("不做猜测补全", "猜测补全", "统一显示", "内部实现", "口径统一")

# ── 通用规则 ────────────────────────────────────────────────────────────────
JARGON = ("SKU", "§", "门户口径", "范围内", "哈希", "幂等", "入库", "抓取")
IMPLEMENTATION_PHRASES = (
    "不做猜测补全", "猜测补全", "统一显示", "内部实现", "落库", "口径统一",
    "抓取脚本", "抓取失败", "字段映射",
)
SKIP_PREFIX = ("//", "{/*", "*", "/**", "/*")

# ── 全局 footer 文案重复检查 ────────────────────────────────────────────────
# 唯一真源：全局 footer（全站覆盖）。页面正文再写一遍 = 用户看到两遍 → FAIL。
FOOTER_FILE = "web/app/layout.tsx"  # 期望位置；实际按 <footer 标签自动定位（见 _footer_source）
FOOTER_MARKER = "<footer"           # 只取 footer 区间的文案，不把 metadata 描述算进来
FOOTER_CLOSE = "</footer>"
FOOTER_MIN_CLAUSE = 8               # 子句长度下限（字符数）：短词（隐私政策/仅供参考）不判重
CLAUSE_SEPARATORS = "，。；、|·,.;:!?！？（）()「」《》\u3000\"'"
# 标签/表达式/注释的边界：既当分隔符（避免相邻段落粘成一句），也在比对时按需抹平
BOUNDARY = "\x00"
CJK = re.compile(r"[\u4e00-\u9fff]")
EXIT_NO_FOOTER = 2                  # 定位不到唯一 footer → 门禁无法判定，退出码 2（不得算通过）


def _read_source(path: pathlib.Path) -> str:
    """读源码；非 UTF-8 时抛出可读错误（由调用方转成 FAIL，而不是打印 traceback）。"""
    return path.read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    """去掉注释：块注释（`/* */`、`{/* */}`、JSDoc）整体替换为 BOUNDARY，`//` 整行注释按行丢弃。

    注释里提到 footer 文案不算「用户看到两遍」。块注释必须整体处理：若只按行丢弃，
    多行 `{/*` + 不以 `*` 开头的续行 + `*/}` 会把中间那行当正文留下（实测误报）。
    行尾注释不处理——按 `//` 切会误伤 URL（取舍已记录，代价是行尾注释里逐字抄 footer 子句会误报）。
    """
    text = re.sub(r"/\*.*?\*/", BOUNDARY, source, flags=re.S)
    keep = [line for line in text.splitlines() if not line.strip().startswith(("//", "*"))]
    return "\n".join(keep)


QUOTED_IN_TAG = re.compile(r'"([^"\n]{2,})"|\'([^\'\n]{2,})\'')


def _visible_text(source: str) -> str:
    """抽出「用户能看到的文字」：去掉标签与空白，但保留标签属性里的字符串。

    去空白是必要的：JSX 里同一句话常被换行与缩进切开，逐字比对前必须先拼回一行。
    标签替换为 BOUNDARY 而不是空格，否则相邻段落的句子会粘成一句、判不出重复。
    **属性里的引号字符串要留下来**：`dangerouslySetInnerHTML={{ __html: "…" }}`、`title="…"`
    这类写法承载的就是用户可见文案，整段吃掉会让门禁 fail-open（实测漏判）。
    刻意**不剥 `{…}` 表达式**：`\\{[^{}]*\\}` 这类正则会被嵌套大括号带偏（曾把整段 JSX
    文案整段吃掉 → 漏判），而表达式残留最多产生不含中文的噪声子句（由 CJK 过滤挡掉）。
    代价是丢掉行号，命中位置由 `_locate` 用前缀回查。
    """
    source = _strip_comments(source)

    def tag_repl(match: re.Match[str]) -> str:
        values = [a or b for a, b in QUOTED_IN_TAG.findall(match.group(0))]
        return BOUNDARY + "".join(values) + BOUNDARY

    text = re.sub(r"<[^>]*>", tag_repl, source)
    text = re.sub(r"\s+", "", text)                                  # 源码换行与缩进
    return re.sub(f"{BOUNDARY}+", BOUNDARY, text)


def _footer_bounds(source: str) -> tuple[int, int] | None:
    """footer 区间 `[start, end)`；`</footer>` 缺失时返回 None（结构不完整，由 `_footer_source` 报错）。"""
    start = source.find(FOOTER_MARKER)
    if start < 0:
        return None
    end = source.find(FOOTER_CLOSE, start)
    if end < 0:
        return None
    return start, end + len(FOOTER_CLOSE)


def _footer_source(root: pathlib.Path) -> tuple[str | None, str]:
    """定位承载全局 footer 的文件：优先期望路径，否则全前端搜唯一含 `<footer` 的 .tsx。

    候选只认 `.tsx`（JSX 只可能出现在 .tsx）且必须同时出现 `<footer` 与 `</footer>`，
    避免 `const TPL = "<footer>"` 这类字符串被当成真源。
    返回 `(相对路径 | None, 说明)`；None 表示定位不到唯一来源——此时门禁**无法判定**，
    必须显式失败而不是静默放行（重构把 footer 拆成组件、或改标签名都会走到这里）。
    """
    def has_footer(path: pathlib.Path) -> bool:
        text = _strip_comments(_read_source(path))
        return FOOTER_MARKER in text and FOOTER_CLOSE in text

    preferred = root / FOOTER_FILE
    if preferred.is_file():
        try:
            if has_footer(preferred):
                return FOOTER_FILE, "期望路径"
        except UnicodeDecodeError:
            return None, f"{FOOTER_FILE} 不是合法 UTF-8，无法解析"
    candidates: list[str] = []
    incomplete: list[str] = []
    for base in UI_ROOTS:
        directory = root / base
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.tsx")):   # 只认 .tsx：JSX 不会写在 .ts 里
            rel = path.relative_to(root).as_posix()
            if "/ops/" in rel or rel.startswith("ops/") or rel in INTERNAL_FILES:
                continue
            try:
                text = _strip_comments(_read_source(path))
            except UnicodeDecodeError:
                incomplete.append(f"{rel}（非 UTF-8）")
                continue
            if FOOTER_MARKER in text and FOOTER_CLOSE in text:
                candidates.append(rel)
            elif FOOTER_MARKER in text:
                incomplete.append(f"{rel}（有 `{FOOTER_MARKER}` 但缺 `{FOOTER_CLOSE}`）")
    if len(candidates) == 1:
        return candidates[0], "按 <footer 标签定位"
    if not candidates:
        if incomplete:
            return None, f"footer 结构不完整，无法解析：{incomplete}"
        return None, f"{UI_ROOTS} 下找不到含 `{FOOTER_MARKER}` 的 .tsx"
    return None, f"含 `{FOOTER_MARKER}` 的文件不唯一：{candidates}"


def footer_clauses(root: pathlib.Path) -> list[str]:
    """从全局 footer 抽出需要判重的子句（长度达标、含中文、去重、保序）。

    口径：只判**逐字重复**的完整子句；长度 < `FOOTER_MIN_CLAUSE` 的子句（如「隐私政策」
    「仅供参考」）天然会重复，不进判重集合，因此**不会**因这些短句报错。
    注意这是**子串**判定：短子句被一段更长的良性文案包含（如 className 里恰好连写出该子句）
    也会命中——方向是 fail-closed（宁可让人来确认，也不放过），出现时按提示改写即可。
    """
    rel, _why = _footer_source(root)
    if rel is None:
        return []
    try:
        stripped = _strip_comments(_read_source(root / rel))
    except UnicodeDecodeError:
        return []
    bounds = _footer_bounds(stripped)
    if bounds is None:
        return []
    footer_text = _visible_text(stripped[bounds[0]:bounds[1]])
    clauses: list[str] = []
    for chunk in re.split(f"[{re.escape(CLAUSE_SEPARATORS + BOUNDARY)}]", footer_text):
        clause = chunk.strip()
        if len(clause) < FOOTER_MIN_CLAUSE or not CJK.search(clause):
            continue
        if clause not in clauses:
            clauses.append(clause)
    return clauses


def _locate(path: pathlib.Path, clause: str) -> int | None:
    """在源文件里找出该子句所在行；找不到就返回 None（宁可没行号，也不报错行号）。"""
    try:
        lines = _read_source(path).splitlines()
    except UnicodeDecodeError:
        return None
    for size in (8, 6, 4):
        head = clause[:size]
        if not head:
            continue
        for lineno, line in enumerate(lines, 1):
            if head in re.sub(r"\s+", "", line):
                return lineno
    return None


def _footer_duplicate_hits(root: pathlib.Path, files: set[pathlib.Path]) -> list[str]:
    """其余前端文件里是否又把 footer 的话说了一遍。

    footer 所在文件本身要排除——但**只排除 footer 区间**：正文里再写一遍同样算重复。
    """
    rel, why = _footer_source(root)
    clauses = footer_clauses(root)
    if rel is None or not clauses:
        return []            # 无法判定：由 main() 以 EXIT_NO_FOOTER 显式失败，不在这里伪装成通过
    hits: list[str] = []
    for path in sorted(files):
        rel_path = path.relative_to(root).as_posix()
        if "/ops/" in rel_path or rel_path.startswith("ops/") or rel_path in INTERNAL_FILES:
            continue
        try:
            stripped = _strip_comments(_read_source(path))
        except UnicodeDecodeError as err:
            hits.append(f"{rel_path}: [编码错误] 非 UTF-8，无法检查 footer 重复（{err}）")
            continue
        if rel_path == rel:                     # 只看 footer 区间之外的正文
            bounds = _footer_bounds(stripped)
            stripped = stripped[: bounds[0]] + stripped[bounds[1]:] if bounds else stripped
        text = _visible_text(stripped)
        # flat：抹平边界后再比对，兼容「同一句被标签/变量切开」的写法
        # 代价：跨标签拼出来的字面序列也可能命中（in<span>本站不提供</span>…），属 fail-closed
        flat = text.replace(BOUNDARY, "")
        for clause in clauses:
            if clause in text or clause in flat:
                lineno = _locate(path, clause)
                where = f"{rel_path}:{lineno}" if lineno else rel_path
                hits.append(
                    f"{where}: [footer 重复] 「{clause}」已在全局 footer "
                    f"({rel}，{why}) 出现，页面内请删除或改写"
                )
    return hits


def _ui_files(root: pathlib.Path) -> set[pathlib.Path]:
    """前端用户界面文件清单（页面 + 前端 lib）。"""
    files: set[pathlib.Path] = set()
    for base in UI_ROOTS:
        directory = root / base
        if directory.is_dir():
            files.update(directory.rglob("*.tsx"))
            files.update(directory.rglob("*.ts"))
    return files


def _frontend_hits(root: pathlib.Path, files: set[pathlib.Path] | None = None) -> list[str]:
    hits: list[str] = []
    for path in sorted(files if files is not None else _ui_files(root)):
        rel = path.relative_to(root).as_posix()
        if "/ops/" in rel or rel.startswith("ops/") or rel in INTERNAL_FILES:
            continue
        try:
            lines = _read_source(path).splitlines()
        except UnicodeDecodeError as err:
            hits.append(f"{rel}: [编码错误] 非 UTF-8，无法检查（{err}）")
            continue
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith(SKIP_PREFIX):
                continue
            matched = next((t for t in JARGON if t in line), None)
            if matched is None:
                matched = next((p for p in IMPLEMENTATION_PHRASES if p in line), None)
            if matched is not None:
                hits.append(f"{rel}:{lineno}: [{matched}] {stripped[:110]}")
    return hits


def _backend_hits(root: pathlib.Path) -> list[str]:
    """只扫**引号内的中文串**（用户可见文案）。

    这样能区分三类：
    - 用户可见：`raise bad_request("SKU 不存在")`、`label = "门户口径"` → 命中；
    - 技术标识符：`SKU_API = "https://…"`、`SKU_PAGE_TEMPLATE` → 术语在引号外 → 不命中；
    - 注释与 docstring：整行跳过（技术术语是给维护者的）。
    """
    hits: list[str] = []
    base = root / BACKEND_ROOT
    if not base.is_dir():
        return hits
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(_read_source(path))
        except SyntaxError as err:
            hits.append(f"{rel}: [SyntaxError] 无法解析：{err.msg}")
            continue
        except UnicodeDecodeError as err:
            hits.append(f"{rel}: [编码错误] 非 UTF-8，无法解析（{err}）")
            continue
        # docstring 语义排除（module/class/function 的首个字符串表达式）
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    if isinstance(body[0].value.value, str):
                        docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            text = node.value
            matched = next((t for t in BACKEND_JARGON if t in text), None)
            if matched is None:
                matched = next((p for p in BACKEND_PHRASES if p in text), None)
            if matched is not None:
                hits.append(f"{rel}:{node.lineno}: [{matched}] {text.strip()[:110]}")
    return hits


def scan(root: pathlib.Path | None = None) -> list[str]:
    root = root or pathlib.Path(__file__).resolve().parents[1]
    files = _ui_files(root)
    return (
        _frontend_hits(root, files)
        + _footer_duplicate_hits(root, files)
        + _backend_hits(root)
    )


def main(argv: list[str] | None = None, root: pathlib.Path | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    verbose = "--verbose" in args
    root = root or pathlib.Path(__file__).resolve().parents[1]
    hits = scan(root)
    for hit in hits if verbose else hits[:20]:
        print(f"  {hit}", file=sys.stderr)
    if not verbose and len(hits) > 20:
        print(f"  …（共 {len(hits)} 处，--verbose 查看全部）", file=sys.stderr)
    # 门禁不许静默失守：定位不到唯一 footer（改名/拆组件/改标签）时无法判定 → 显式失败。
    # 注意：已发现的 hits 必须先打印出来，否则退出码 2 会把内部术语等真实命中一起吞掉。
    footer_rel, why = _footer_source(root)
    if footer_rel is None:
        print(f"[FAIL] footer 重复检查无法判定：{why}", file=sys.stderr)
        print(
            "        请确认全局 footer 仍在 web/app 下且只出现一次；确属结构调整时，"
            "同步更新 reviewer/scan_ui_copy.py 的 FOOTER_MARKER/FOOTER_FILE 口径",
            file=sys.stderr,
        )
        return EXIT_NO_FOOTER
    if not footer_clauses(root):
        print(
            f"[FAIL] footer 重复检查无法判定：{footer_rel} 的 footer 区间没抽到可判定的子句"
            f"（区间为空，或全部短于 {FOOTER_MIN_CLAUSE} 字）",
            file=sys.stderr,
        )
        return EXIT_NO_FOOTER
    if not hits:
        # 只用 ASCII 标记：Windows 控制台默认 GBK，打印 ✓ 会 UnicodeEncodeError（实测）
        print(f"[OK] 前端与后端用户可见文案无内部术语与实现说明，footer 文案无重复（{footer_rel}）")
        return 0
    print(f"[FAIL] 发现 {len(hits)} 处文案问题（内部表述 / footer 重复）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
