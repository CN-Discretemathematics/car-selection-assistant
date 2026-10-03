r"""两处口径统一的**不变式**（2026-10-03 产品拍板后落地）。

## B4｜引用条数：盘点回答不再不限条

`_source_citations` 此前带一个 `limit: int | None` 参数，盘点回答传 `limit=None`
（不限条），其余三条走默认 `CITATION_LIMIT=3`——同一页面上两套引用密度。

现在**删掉了 `limit` 参数**、四个调用点全部走 `CITATION_LIMIT`。留着一个
「可不传上限」的口子，正是当初那条分歧得以存在的原因，所以这次连口子一起封。

## B6｜avoid 路径统一小写匹配

偏好路径用小写化的 `msg_low`，avoid（排除项）路径却用**原始 message**、大小写敏感，
于是 `_BODY_HINTS` 被迫同时保留「MPV」与「mpv」两个键。当时功能没坏（两键并存时
大小写输入都命中），但它**看起来像冗余**——下一个人删掉大写键，「不要MPV」就会
静默漏掉排除项。现在两条路径都小写化，大写键已删除。

## 写这个文件时被自己坑了第四次（留作提醒）

第一版断言里：
  - 用 `def name` 加括号的正则抓函数，**只抓到签名、拿不到函数体**，
    于是「函数体里必须有 CITATION_LIMIT」这条正确断言报了红；
  - 调用点正则把 `def` 那一行也算进去，4 个调用点数成 5；
  - 「不得出现 `in message`」把 `"燃油" in message`、`_USAGE_HINTS … in message`
    这些**中文键的合法匹配**也误伤了——中文没有大小写，原串匹配是对的；
  - docstring 里直接写正则片段又没加 r 前缀，ruff 首跑就报两条 W605
    （左括号与 S 前的反斜杠是无效转义）——**文档里的正则片段也要用 raw 串**。
    修这一条时又连踩两次自指的坑：先是**描述 W605 的那句话本身含无效转义**，
    于是本模块的 docstring 改成了 raw 串；改完又发现**在同一段文字里写出
    三引号本身**，把 docstring 提前闭合、整个文件结构坏掉（pytest 直接
    collection error、ruff 从 129 飙到 200）。教训：**在 docstring 里谈论
    docstring 语法时，不要把语法符号本身抄进去**。

四次都是**测试写错、代码是对的**。断言对正确代码报红比没有断言更糟：它会诱导
去改本来没问题的代码。所以这里的正则一律只针对「相关的那几行」，不做全文匹配。
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2] / "backend" / "app" / "agent" / "engine.py").read_text(
    encoding="utf-8"
)


def _body(name: str) -> str:
    """抓整个函数体，并**剥掉 docstring**。

    三个坑，都踩过：
      1. 按「下一个 def/class/@」切会**越界**——真实下一个函数前面通常还跟着注释块；
      2. 按「首个非缩进行」切会**早停**——多行签名的收尾 `) -> list[Citation]:`
         就在第 0 列；
      3. 不剥 docstring 的话，**文档本身**会触发代码断言——本函数的 docstring 里
         就有一张历史对照表写着 `source_ids[:3]`，于是「不得出现字面量上界」
         这条正确的代码断言报红。
    不变式该管代码，不管文档。
    """
    lines = _SRC.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"def {name}("))
    i = start
    while i < len(lines) and not re.match(
        r"^\s*\)\s*(->[^:]*)?:\s*$|^def \w+\(.*\)\s*(->[^:]*)?:\s*$", lines[i]
    ):
        i += 1
    i += 1  # 跳过签名收尾
    out: list[str] = []
    if i < len(lines) and lines[i].lstrip().startswith(('"""', "'''")):
        quote = lines[i].lstrip()[:3]
        i += 1
        while i < len(lines) and quote not in lines[i]:
            i += 1
        i += 1
    for ln in lines[i:]:
        if ln.strip() and not ln[0].isspace():
            break
        out.append(ln)
    return "\n".join([lines[start]] + out)


def _match_targets(*tables: str) -> list[str]:
    """取出这些 hints 表**所有**消费方用于 `in` 匹配的目标变量。

    两种写法都要覆盖：`for … : if key in X`（两行式）与
    `[… for key, value in T.items() if key in X]`（推导式）。

    实现刻意用**逐行扫描**而非一个大正则：第一版的大正则里 `[^\n]*` 会抢先把
    `if key in X` 吃掉，于是 `in` 匹配到的是 for 语句里的 `in _ENERGY_HINTS`，
    断言数出 5 个表名而非 5 个目标变量，报错的却是代码。
    """
    markers = tuple(f"{t}.items()" for t in tables)
    lines = _SRC.splitlines()
    targets: list[str] = []
    for idx, ln in enumerate(lines):
        if not any(m in ln for m in markers):
            continue
        window = ln
        # 两行式：匹配语句在下一行
        if "if " not in ln and idx + 1 < len(lines):
            window += "\n" + lines[idx + 1]
        hits = re.findall(r"\bin\s+(\w+)", window)
        if hits:
            targets.append(hits[-1])
    return targets


def _hint_table(name: str) -> list[str]:
    m = re.search(rf"^{name} = \{{(.*?)\}}", _SRC, re.M | re.S)
    assert m, f"找不到 {name}"
    return re.findall(r'"([^"]+)"\s*:', m.group(1))


# ── B4 引用上限 ──────────────────────────────────────────────────────────────

def test_source_citations_no_longer_accepts_a_limit_argument():
    """不留「可不传上限」的口子——那正是分歧得以存在的原因。"""
    fn = _body("_source_citations")
    assert not re.search(r"^\s*limit\s*:", fn, re.M), (
        "_source_citations 仍声明 limit 参数：将来有人再传 limit=None 就复活旧分歧"
    )


def test_no_call_site_passes_limit():
    calls = re.findall(r"(?<!def )_source_citations\((?:[^()]|\([^()]*\))*\)", _SRC, re.S)
    assert len(calls) == 4, f"应有 4 个调用点，实际 {len(calls)}"
    for c in calls:
        assert "limit" not in c, f"调用点仍在传 limit：{c[:80]}"


def test_citation_limit_constant_is_the_single_source():
    """截断必须引用常量而非字面量——常量改了才跟着改。"""
    fn = _body("_source_citations")
    assert "source_ids[:CITATION_LIMIT]" in fn, "截断必须走 CITATION_LIMIT 常量"
    assert not re.search(r"source_ids\[:\d+\]", fn), "不得出现字面量上界"


# ── B6 大小写对称 ───────────────────────────────────────────────────────────

def test_body_hints_has_no_uppercase_key():
    """「MPV」大写键已删除——两条路径都小写化后它没有存在理由。"""
    keys = _hint_table("_BODY_HINTS")
    upper = [k for k in keys if k != k.lower()]
    assert not upper, f"仍存在大写键 {upper}：它只在某条路径不小写化时才需要"
    assert "mpv" in keys, "小写 mpv 键必须在"


def test_energy_hints_keys_are_case_insensitive_by_construction():
    """能源表全是中文键，天然无大小写问题——顺带钉住，免得将来加英文键时无人察觉。"""
    keys = _hint_table("_ENERGY_HINTS")
    upper = [k for k in keys if k != k.lower()]
    assert not upper, (
        f"_ENERGY_HINTS 出现了 {upper}：avoid 路径已改为小写化匹配，"
        "大写键在这里将永远匹配不到（与 _BODY_HINTS 当初的 MPV 同一个坑）"
    )


def test_body_and_energy_hint_loops_all_match_lowercased_text():
    """两张表的**逐行式**消费方都必须在小写化文本上匹配。

    2026-10-03 否定句重写：avoid 路径的 2 处消费方被搬进 `_negated_values()`
    （全句级判定 → 逐关键词判定，见提案 §4.3），故此处由 5 处变 3 处。
    搬走的那处**并没有失去约束**——见下面两条专门针对它的测试。
    """
    targets = _match_targets("_BODY_HINTS", "_ENERGY_HINTS")
    assert len(targets) == 3, f"应恰好 3 处 hints 匹配消费方，实际 {len(targets)}: {targets}"
    for t in targets:
        assert t in ("msg_low", "low"), (
            f"hints 匹配用了 `{t}`——必须是小写化文本（msg_low/low），"
            "否则大写输入会静默漏匹配"
        )


def test_negated_values_receives_lowercased_text():
    """`_negated_values` 必须是小写化文本的消费者。

    它不再写成 `if key in X` 两行式（整段判定被重写），所以上面那条扫描器
    看不见它——**不变式的覆盖出现空洞**。这里显式补上：判定函数的入参名一旦
    从 msg_low 改成 message，大写输入就会静默漏匹配。

    用正则直接扫源文件而不用 `_body()`：那个取函数体的辅助函数对 `extract_hints`
    实测返回了**别的函数**的内容，拿它断言等于没断言。
    """
    assert re.search(r"_negated_values\(\s*msg_low\s*\)", _SRC), (
        "否定句判定必须传 msg_low；传 message 会让「不要MPV」大小写敏感"
    )


def test_negated_values_lowercases_the_hint_key():
    """入参是小写化文本，键也要小写化——两个条件缺一不可。

    只做其中一半是中文表的老坑（`_BODY_HINTS` 当初被迫留「MPV」「mpv」双键）。
    """
    body = re.search(
        r"^def _negated_values\(.*?(?=^\S)", _SRC, re.M | re.S
    )
    assert body, "找不到 _negated_values 定义"
    fn = body.group(0)
    assert "key.lower()" in fn, "提示词键未小写化，大写/混合大小写输入会静默漏匹配"
