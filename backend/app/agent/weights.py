"""评分维度的**单一归属**：8 维定义、默认权重、上限、增量、关键词词表、用途词表。

## 为什么有这个 module

2026-10-20 之前，这些知识散在**三个** module：

| 知识 | 原属 | 被谁反向 import |
| --- | --- | --- |
| `DEFAULT_WEIGHTS` / `WEIGHT_CEILING` / `_DIM_LABELS` | `agent/tools.py` | engine、soft_prefs、soft_probe |
| `_WEIGHT_RAISE` | `agent/engine.py`（私有） | **soft_prefs、soft_probe**（函数内延迟 import） |
| `_USAGE_HINTS` | `agent/engine.py`（私有） | soft_prefs |

于是形成了**真实的循环依赖**：

    engine.py (顶层) ──import──▶ soft_prefs.py
                                      │
                                      └──函数内延迟 import──▶ engine.py

Python 不允许顶层互相 import，所以下游只能**在函数体内**import 上游——
那 4 处延迟 import（`soft_prefs.py:253/260/267/728/729/782`、`soft_probe.py:99`）
**不是设计，是循环依赖的疤痕**。

更糟的是团队自己知道风险，写在 `soft_prefs.py:723`：

> WEIGHT_CEILING 上限、累加语义全部复用正则路径那份实现。**两处各写一份必然漂移**

但它选择用「延迟 import 打补丁」绕过，而不是收拢 module。**补丁挡住了崩溃，
没挡住漂移。**

## 现在

所有权重知识归本 module，四个消费方（`engine` / `tools` / `soft_prefs` /
`soft_probe`）各自 import，**环自然消失，延迟 import 一并消失**。

本 module **不 import 任何 app.* 模块**——它是叶子，不制造新环。

## 权重语义的要点（别改错）

- `profile.weights` 里的值是**绝对权重（覆盖）**，不是累加器。
  API 直传权重（`POST /recommendations {"weights": {...}}`）依赖这一点。
  所以强调某维度时发的是「默认值 + 增量」的**绝对值**，
  发裸增量会把默认本就高于增量的维度**反向压低**——
  「主要看价格」→ budget 由 0.30 掉到 0.20，用户越强调价格系统越不看重价格
  （2026-10-03 实测发现，此前注释写「增量/叠加」而代码是覆盖）。
- `WEIGHT_CEILING` 防止重复强调把权重累加到 3.0——
  那等于「只看这一个维度」，与综合推荐的产品意图相悖。
"""
from __future__ import annotations

#: 8 个**真被测量**的评分维度 → 面向用户的中文名。
#:
#: ⚠️ 语义标签与推荐卡片的角色标签（best_fit / budget_safest / …）**不是一套东西**：
#: 这里的是「维度名」，角色是「按分数派生的角色」，二者不可混用。
DIM_LABELS: dict[str, str] = {
    "budget": "预算吻合度",
    "usage": "用途匹配",
    "space": "空间",
    "energy": "能耗",
    "power": "动力",
    "comfort": "舒适性配置",
    "intelligence": "智能化配置",
    "maintenance": "维护便利性",
}

#: 8 维的默认权重。**绝对权重**，不是增量。
DEFAULT_WEIGHTS: dict[str, float] = {
    "budget": 0.30,
    "usage": 0.15,
    "space": 0.10,
    "energy": 0.15,
    "power": 0.10,
    "comfort": 0.05,
    "intelligence": 0.05,
    "maintenance": 0.10,
}

#: 单维权重上限。超过它意味着「只看这一个维度」，与「综合推荐」的产品意图相悖。
#: 重复说同一句强调会跨轮累加，没有上限时 profile 可以被一句话带到 3.0。
WEIGHT_CEILING: float = 1.0

#: 单次强调的权重**增量**。
#:
#: ⚠️ 发出时必须发「默认值 + 增量」的**绝对值**，不能发裸增量——
#: 原因见本 module 文档字符串。
WEIGHT_RAISE: float = 0.2

#: 落后幅度小于此值不算取舍——0.01 的差距是噪声，写出来只会稀释真正的信息。
#: 属「取舍叙事」阈值，与权重无关，但同属「打分口径」这一族知识，故放在一起。
TRADEOFF_GAP: float = 0.2

#: 用途关键词 → 用途值。`profile.usage` 取的是**值**（「通勤」/「家庭」/「长途」/「商务」）。
USAGE_HINTS: dict[str, str] = {
    "上下班": "通勤",
    "通勤": "通勤",
    "日常": "通勤",
    "代步": "通勤",
    "每天": "通勤",
    "平时": "通勤",
    "上班": "通勤",
    "家用": "家庭",
    "家庭": "家庭",
    "带娃": "家庭",
    "接送孩子": "家庭",
    "接送": "家庭",
    "买菜": "家庭",
    "家人": "家庭",
    "长途": "长途",
    "自驾": "长途",
    "旅游": "长途",
    "出差": "商务",
    "商务": "商务",
    "接待": "商务",
    "办公": "商务",
}

#: 评分维度关键词 → 8 维权重键。
#:
#: 2026-10-03（sales-agent-proposal L2 补齐）：新增词条的判定标准只有一条——
#: **这个维度在 recommendation_tool 里真的被计算，且这一轮真的被测到。**
#: sales-agent-proposal §2 举的典型销售话术「我最看重后排和家用」此前两个都落空：
#: 「后排」不在表里，「家用」在 USAGE_HINTS 里但没有权重入口，用户的强调被静默丢弃。
WEIGHT_DIM_KEYWORDS: tuple[tuple[str, str], ...] = (
    # 用途：8 维里**唯独没有关键词入口**的维度（DEFAULT_WEIGHTS 给它 0.15，最高）。
    # 词表取 USAGE_HINTS 键的**子集**，这是刻意的结构约束：只有同时也会写进
    # profile.usage 的词，才能让 tools 侧把 usage 计入 measured 集合。
    # 给 usage 加权却测不到 = 白白稀释其它维度而换不来任何排序变化。
    # 该不变量由 tests/test_agent.py::test_usage_weight_keywords_are_measurable 钉住。
    ("通勤", "usage"), ("上下班", "usage"), ("代步", "usage"),
    ("家用", "usage"), ("家庭", "usage"), ("带娃", "usage"),
    ("接送", "usage"), ("买菜", "usage"),
    ("长途", "usage"), ("自驾", "usage"), ("旅游", "usage"),
    ("出差", "usage"), ("商务", "usage"),
    # 空间（space 由 length_mm 车长归一）：补销售高频的「后排/坐人」说法
    ("大空间", "space"), ("空间", "space"), ("后排", "space"),
    ("坐人", "space"), ("载人", "space"),
    ("动力", "power"), ("性能", "power"), ("马力", "power"), ("加速", "power"),
    ("续航", "energy"), ("油耗", "energy"), ("能耗", "energy"), ("电耗", "energy"),
    ("智驾", "intelligence"), ("智能", "intelligence"), ("辅助驾驶", "intelligence"),
    ("车机", "intelligence"), ("科技", "intelligence"),
    ("舒适", "comfort"), ("舒服", "comfort"), ("隔音", "comfort"),
    ("保养", "maintenance"), ("维修", "maintenance"), ("省心", "maintenance"),
    ("售后", "maintenance"), ("网点", "maintenance"), ("配件", "maintenance"),
    ("性价比", "budget"), ("价格", "budget"),
)
# ⚠️ 以下说明只适用于 **v1 八维**（AGENT_SCORING_MODE=off）。
# v2 已新增真正读安全配置事实的 safety 维，词表见下方 WEIGHT_DIM_KEYWORDS_V2；
# 旧红线的豁免记录见 docs/adr/0008-scoring-v2-seven-dimensions.md。
#
# v1 里「安全 / 碰撞 / 气囊」**刻意不映射到任何维度**（契约由测试钉住，见
# tests/test_agent.py::test_safety_emphasis_is_deliberately_unmapped）：
# 8 个维度的公式里**没有任何一个读安全类 fact_key**——budget←价格、usage←用途×车身、
# space←length_mm、energy←能源类型、power←功率、comfort/intelligence←配置类事实键、
# maintenance←品牌规模+销量活跃度。把「我最看重安全」映射到其中任何一个，都不会让排序
# 更安全，只会让用户以为系统听懂了他最在意的东西——**用行为冒充理解**，比不回答更糟。
# 真要支持，需要先加一个真读安全事实的维度（数据 + 公式 + 归一化），属新增能力，
# 不是往这张表里加词能解决的。

# ── 评分 v2（七维、固定锚点、全部同价位比较）────────────────────────────────
#
# 2026-10-27 用户决策：见 docs/2026-10-27-scoring-v2-design.md 与 ADR-0008。
# v1 八维常量保留在文件上半部分，供 AGENT_SCORING_MODE=off 的旧内核使用；
# v2 用下面的七维常量。两套常量同时存在是**刻意的**：开关 off 必须逐位回到
# 旧行为，不能靠改默认权重“顺便”切换。
SCORING_VERSION_V1: str = "scoring-v1"
SCORING_VERSION_V2: str = "scoring-v2"

#: v2 七维（顺序即产品展示顺序）。
SCORING_V2_DIM_KEYS: tuple[str, ...] = (
    "safety",
    "running_cost",
    "space",
    "power",
    "comfort",
    "intelligence",
    "usage",
)

DIM_LABELS_V2: dict[str, str] = {
    "safety": "安全配置水平",
    "running_cost": "用车成本",
    "space": "空间",
    "power": "动力",
    "comfort": "舒适配置",
    "intelligence": "智能配置",
    "usage": "用途适配",
}

#: v2 默认权重（归一化前）。用途维未激活时从分母移除，其余维按比例归一。
DEFAULT_WEIGHTS_V2: dict[str, float] = {
    "safety": 0.22,
    "running_cost": 0.18,
    "space": 0.15,
    "power": 0.13,
    "comfort": 0.12,
    "intelligence": 0.10,
    "usage": 0.10,
}

#: v2 的强调增量与 v1 相同；单独命名是为了让“两套口径”在代码里也看得见。
WEIGHT_RAISE_V2: float = WEIGHT_RAISE

#: v2 的价格带（万元，左闭右开；最后一档无上界）。所有七维都按同价位锚点比较；
#: 样本不足的价格带在锚点文件里回退到全库锚点并标记 fallback。
PRICE_BANDS_V2: tuple[tuple[float, float | None], ...] = (
    (0.0, 8.0),
    (8.0, 10.0),
    (10.0, 12.0),
    (12.0, 15.0),
    (15.0, 18.0),
    (18.0, 22.0),
    (22.0, 28.0),
    (28.0, 35.0),
    (35.0, 50.0),
    (50.0, None),
)

#: v1 键 → v2 键的兼容映射。budget/maintenance 在 v2 中没有对应维度，忽略；
#: energy 的语义由“能源类型”变为“用车成本”，映射到 running_cost。
V2_WEIGHT_KEY_ALIASES: dict[str, str] = {
    "energy": "running_cost",
}

#: v2 的强调词表。与 v1 的差异只有三条产品决策：
#: 1. 安全/碰撞/气囊**有入口**了——因为 v2 真有一个读安全配置事实的维度；
#: 2. 续航/油耗/能耗/电耗映射到 running_cost（v2 里它就是按官方参数估算的用车成本）；
#: 3. 保养/维修/省心并入 running_cost；budget/maintenance 在 v2 已不是评分维度，
#:    不再给权重入口（预算只做硬约束，养护不再是可测量维度）。
#: usage 的词表仍取 USAGE_HINTS 子集：只有同时会写进 profile.usage 的词，才能让
#: usage 维进入 measured 集合（加权重却测不到 = 白白稀释其它维度）。
WEIGHT_DIM_KEYWORDS_V2: tuple[tuple[str, str], ...] = (
    ("通勤", "usage"), ("上下班", "usage"), ("代步", "usage"),
    ("家用", "usage"), ("家庭", "usage"), ("带娃", "usage"),
    ("接送", "usage"), ("买菜", "usage"),
    ("长途", "usage"), ("自驾", "usage"), ("旅游", "usage"),
    ("出差", "usage"), ("商务", "usage"),
    ("大空间", "space"), ("空间", "space"), ("后排", "space"),
    ("坐人", "space"), ("载人", "space"),
    ("动力", "power"), ("性能", "power"), ("马力", "power"), ("加速", "power"),
    ("续航", "running_cost"), ("油耗", "running_cost"), ("能耗", "running_cost"),
    ("电耗", "running_cost"), ("省油", "running_cost"), ("省电", "running_cost"),
    ("用车成本", "running_cost"), ("使用成本", "running_cost"),
    ("养车成本", "running_cost"), ("油费", "running_cost"), ("电费", "running_cost"),
    ("保养", "running_cost"), ("维修", "running_cost"), ("省心", "running_cost"),
    ("安全", "safety"), ("安全性", "safety"), ("碰撞", "safety"), ("气囊", "safety"),
    ("主动安全", "safety"), ("被动安全", "safety"),
    ("智驾", "intelligence"), ("智能", "intelligence"), ("辅助驾驶", "intelligence"),
    ("车机", "intelligence"), ("科技", "intelligence"),
    ("舒适", "comfort"), ("舒服", "comfort"), ("隔音", "comfort"),
)


def default_weights_for_mode(mode: str) -> dict[str, float]:
    """按评分模式取默认权重；未知 mode 按 off 处理（fail safe）。"""
    if mode == SCORING_VERSION_V2:
        return dict(DEFAULT_WEIGHTS_V2)
    return dict(DEFAULT_WEIGHTS)


def dim_labels_for_mode(mode: str) -> dict[str, str]:
    """按评分模式取维度标签；v2 同时接受旧键（用于旧画像的兼容展示）。"""
    if mode == SCORING_VERSION_V2:
        return {**DIM_LABELS, **DIM_LABELS_V2}
    return DIM_LABELS


def default_weight_for_dim(dim: str) -> float:
    """某个维度在任一版本里的基础权重（用于判断“这一轮是否被抬高”）。"""
    return max(
        float(DEFAULT_WEIGHTS.get(dim, 0.0)),
        float(DEFAULT_WEIGHTS_V2.get(dim, 0.0)),
    )

