"""回答契约自证（P2）：模型产出的答案在**发送前**先过确定性校验。

原则：理解层可以智能，计算层保持确定性，错误在发送前被自证拦截——
模型答案里的每个数字、每个车系/品牌名都必须能在本轮工具返回的数据里找到依据；
找不到就是编造，先回灌违规项让模型重写一次，仍不合格则回退确定性文案。

入口：
- `numbers_in(text)` / `names_in(text)`：从文本提取「出现过的数值 / 名字候选」；
- `answer_numbers_allowed`：原 engine 的数字白名单校验（迁移至此统一口径，
  engine 侧 re-export，既有调用点不破坏）；
- `validate_tool_answer(answer, allowed_numbers, allowed_names)`：工具循环
  最终答案的契约校验，返回违规描述列表（空 = 合格）；
- `tool_result_universe(result)`：从一次工具返回收集「允许出现的数值与名字」，
  由 `_tool_loop_reply` 在每步工具调用后累积；
- `check_catalog_overview_text(text, overview)`：全库盘点文案的轻量自检
  （正文数字 ⊆ overview 字典数值 ∪ 派生），防止以后文案与数据脱节。

设计取舍（防误拦，宁漏勿错）：
- 数字用「数值集合 + 0.01 容差」比较（沿用 answer_numbers_allowed 的实测口径，
  不用子串匹配——子串会把 200km 误判为命中 20000）；
- 工具循环的数值白名单含 **/10000 万元派生**（工具返回「元」，模型按中文习惯
  说「20 万元」不是编造；与对比分析 allowed_numbers 的派生口径一致）；
- 名字提取只认「CJK 前缀 + 拉丁字母核心」的车名形状（秦PLUS/腾势Z9GT/EQE），
  纯 CJK 车名（凯美瑞）与普通词无法区分，不做形状猜测；纯数字核心（海豹06 的
  「06」）由数字契约兜底。形状候选与允许名集合有**包含关系**即视为同源
  （品牌前缀拼接「丰田凯美瑞」、子串命中都放行）。
"""
from __future__ import annotations

import json
import re
from typing import Any

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_CJK = r"\u4e00-\u9fa5"
# 名字形状候选：可选 CJK 前缀（≤6 字，品牌/厂商字）+ 必含拉丁字母的核心。
# 「必含拉丁字母」是刻意的保守边界：纯 CJK 词无法与普通词区分，纯数字由数字契约兜底。
# 前缀排除连接/语气词（和/与/跟/或…）：避免把「和腾势Z9GT」这类相邻语词吞进候选。
_NAME_SHAPE_RE = re.compile(
    rf"(?:(?![和与跟或及的吗呢吧啊呀哦])[{_CJK}]){{0,6}}[A-Za-z][A-Za-z0-9\-\./]*"
)
# 从形状候选里剥掉 CJK 前缀，得到拉丁核心（按它过滤通用缩写/版本词）
_CJK_PREFIX_RE = re.compile(rf"^[{_CJK}]+")
# 常见非车名的拉丁 token：正常解释里就会出现（车身类别/工况缩写/单位），不应判为编造名字。
# 命中即放行（uppercase 比对）。
_GENERIC_NAME_TOKENS = frozenset(
    {
        "SUV", "MPV", "APP", "EV", "BEV", "PHEV", "EREV", "HEV", "ICE", "DM", "DHT",
        "CLTC", "WLTC", "NEDC", "EPA", "4WD", "AWD",
        "KM", "KWH", "KW", "MM", "KG", "NM", "L", "T",
        # 单独出现不指认任何车型的版本/系列后缀词（词表命中时本就放行，这里双保险）
        "PRO", "MAX", "PLUS", "ULTRA",
    }
)


def numbers_in(text: str) -> set[float]:
    """文本里出现的全部十进制数值（含小数）——与 answer_numbers_allowed 同一提取口径。"""
    return {float(token) for token in _NUMBER_RE.findall(text or "")}


def names_in(text: str, vocabulary: set[str] | None = None) -> set[str]:
    """提取文本里的名字。

    - 给定 vocabulary（如工具返回的车系/品牌名集合）：返回其中**出现在文本里**
      的名字（词表精确命中，是最可靠的名字提取方式）；
    - 未给 vocabulary：保守提取「CJK 前缀 + 拉丁核心」形状的车名候选
      （腾势Z9GT/奔驰EQE），按**拉丁核心**过滤通用缩写（SUV/CLTC/km 与版本词
      PLUS/PRO/MAX…）与单字符。
      两类刻意的保守边界（宁漏勿错，漏判由数字契约兜底）：
      a) 纯 CJK 车名（凯美瑞）与普通词无法区分，不做形状猜测；
      b) 拉丁核心为版本词的车名（秦PLUS）不做名字判定——避免「秦PLUS Pro」
         这类正常表述因裸版本词被误拦。
    """
    text = text or ""
    if vocabulary:
        return {name for name in vocabulary if name and name in text}
    found: set[str] = set()
    for match in _NAME_SHAPE_RE.finditer(text):
        token = match.group(0)
        core = _CJK_PREFIX_RE.sub("", token)
        if len(token) <= 1:
            continue
        if token.upper() in _GENERIC_NAME_TOKENS or core.upper() in _GENERIC_NAME_TOKENS:
            continue
        found.add(token)
    return found


def _number_pool(allowed: set[float] | list[float]) -> set[float]:
    return {round(float(v), 3) for v in (allowed or [])}


def _number_hit(value: float, pool: set[float]) -> bool:
    return any(abs(value - candidate) < 0.01 for candidate in pool)


def answer_numbers_allowed(answer: str, allowed: set[float] | list[float]) -> tuple[bool, str | None]:
    """答案数字校验：回答里的每个数字都必须能在分析结果的数值集合里找到（防编造）。

    用**数值集合 + 容差**比较，不用子串匹配——子串会把「200km」误判为命中「20000」
    （实测踩过）。比 `citation_verifier` 更严：后者校验「引用是否来自证据集」，
    这里校验**数字本身**；模型凭空补一个数会立刻被拦下，改用确定性文案。
    （原 engine 实现，迁移至此统一数字口径；engine 侧 re-export 保持调用点兼容。）
    """
    pool = _number_pool(allowed)
    for token in _NUMBER_RE.findall(answer):
        value = float(token)
        if _number_hit(value, pool):
            continue
        return False, f"答案包含分析结果之外的数字：{token}"
    return True, None


def _name_covered(candidate: str, allowed_names: set[str]) -> bool:
    """名字容差：候选与某允许名互为子串即视为同源（品牌前缀拼接/缩写都放行）。"""
    for name in allowed_names:
        if not name:
            continue
        if candidate in name or name in candidate:
            return True
    return False


def validate_tool_answer(
    answer: str,
    allowed_numbers: set[float] | list[float],
    allowed_names: set[str] | list[str],
) -> list[str]:
    """工具循环最终答案的契约校验，返回违规描述列表（空列表 = 合格）。

    - 每个数字必须落在 allowed_numbers（0.01 容差）；
    - 每个名字形状候选必须与 allowed_names 有包含关系（词表命中天然通过；
      用户消息/会话画像里出现过的名字由调用方先并入 allowed_names）。
    违规描述会原样回灌给模型重写，因此措辞需要让模型知道**错在哪一项**。
    """
    violations: list[str] = []
    pool = _number_pool(allowed_numbers)
    for token in _NUMBER_RE.findall(answer or ""):
        value = float(token)
        if _number_hit(value, pool):
            continue
        violations.append(f"答案包含工具结果之外的数字：{token}")
    allowed = {name for name in (allowed_names or []) if name}
    for candidate in sorted(names_in(answer)):
        if _name_covered(candidate, allowed):
            continue
        violations.append(f"答案包含工具结果之外的车系/品牌名：{candidate}")
    return violations


# 工具返回结果里视为「名字」的键（车系/品牌/款型展示名；不收正文长文本，避免放松口径）
_NAME_VALUE_KEYS = frozenset(
    {"series_name", "brand_name", "display_name", "name", "brand", "series", "title"}
)


def _collect_name_values(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _NAME_VALUE_KEYS and isinstance(value, str) and value.strip():
                out.add(value.strip())
            else:
                _collect_name_values(value, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_name_values(item, out)


def tool_result_universe(result: Any) -> tuple[set[float], set[str]]:
    """从一次工具返回里收集「答案允许出现的数值与名字」。

    数值 = 结果 JSON 里全部数字 + /10000 万元派生（200000 ↔ 20 万元；
    工具返回「元」、模型说「万元」是同一事实的两种口径，不是编造）。
    名字 = 结果里 name 类键的字符串值（series_name/brand_name/display_name…），
    嵌套结构与列表都会展开。
    """
    dump = json.dumps(result, ensure_ascii=False, default=str)
    numbers = numbers_in(dump)
    numbers |= {round(n / 10000, 3) for n in numbers}
    names: set[str] = set()
    _collect_name_values(result, names)
    return numbers, names


def _walk_numbers(node: Any, out: set[float]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _walk_numbers(value, out)
    elif isinstance(node, (list, tuple, set)):
        for value in node:
            _walk_numbers(value, out)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, (int, float)):
        out.add(float(node))


def overview_numbers(overview: dict) -> set[float]:
    """overview 字典（含嵌套/列表）里的全部数值 + /10000 派生（未来文案若带万元口径也不误拦）。"""
    numbers: set[float] = set()
    _walk_numbers(overview, numbers)
    numbers |= {round(n / 10000, 3) for n in numbers}
    return numbers


def check_catalog_overview_text(text: str, overview: dict) -> list[str]:
    """全库盘点文案自检：正文数字 ⊆ overview 数值 ∪ 派生。

    文案由确定性代码从 overview 渲染，正常情况下恒合格；这里是一道**回归绊线**——
    以后有人改文案模板引用了 overview 里没有的数字（或数据键改名），立刻在这里暴露。
    返回违规描述列表（空 = 合格）；调用方按日志告警处理，不打断用户请求。
    """
    pool = overview_numbers(overview)
    problems: list[str] = []
    for token in _NUMBER_RE.findall(text or ""):
        value = float(token)
        if _number_hit(value, pool):
            continue
        problems.append(f"盘点文案包含 overview 数据之外的数字：{token}")
    return problems
