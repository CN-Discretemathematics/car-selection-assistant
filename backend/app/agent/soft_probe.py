"""L4｜软缺口追问：真人会问的那一句（sales-agent-proposal §6 第 4 步）。

## 它补的是 L1 之后最后一块

L1 能听懂「我最看重后排」，但**用户什么都不强调时**（只给硬约束），排序就只剩默认
权重——系统没问过他想要什么。真人销售会在这时候问一句：

> 「你更看重后排空间，还是续航？」

## 三条硬约束

1. **选项必须来自 L1 的封闭枚举**（8 个真被测量的维度，取自 `tools._DIM_LABELS`），
   **不能由 LLM 现编**——本提案的核心纪律，提问也不例外；
2. **不阻塞推荐**。硬约束（预算/用途/人数）缺失时本来就要问，那种问是**必要**的；
   软偏好缺失时问就是**拖沓**。所以这里是**随推荐一起给出**的追问（`followup`），
   前端渲染在推荐卡片下方，点不点都不影响本轮结果；
3. **只问一次，且只在用户没强调过任何东西时问**。用户已经说了「我最看重后排」还问
   「你更看重什么」是听不懂人话；问第二次同理。

## 回答怎么落地

用户点「空间」或直接说「空间」，下一轮由 `apply_probe_answer` 识别并落到权重上——
走的仍是 `to_hints` → `merge_profile` **同一条生产路径**，不另写一套映射。
"""
from __future__ import annotations

from typing import Any, Callable

from app.agent.schemas import Clarification
from app.agent.tools import _DIM_LABELS, DEFAULT_WEIGHTS

# 追问顺序：按画像挑出**最可能有决定性**的维度。
# 判定谓词只读已有画像字段，不新增信息源。
_PROBE_ORDER: tuple[tuple[str, Callable[[Any], bool]], ...] = (
    # 多人/家庭 → 空间几乎总是决定性的
    ("space", lambda p: (p.passengers or 0) >= 3 or "家庭" in (p.usage or [])),
    # 通勤或已指定能源 → 能耗
    ("energy", lambda p: "通勤" in (p.usage or []) or bool(p.energy_preference)),
    ("power", lambda p: True),  # 兜底：恒真，保证凑得出两项
)
# 一次问两项：多了变问卷，少了问不出信息
_PROBE_SIZE = 2


def _pick_dims(profile: Any) -> list[str]:
    """按画像挑两个维度。只在**真被测量**的维度里选（`_DIM_LABELS` 的键）。"""
    picked: list[str] = []
    for dim, when in _PROBE_ORDER:
        if dim in _DIM_LABELS and when(profile) and dim not in picked:
            picked.append(dim)
        if len(picked) >= _PROBE_SIZE:
            break
    return picked


def probe_question(profile: Any) -> Clarification | None:
    """返回软缺口追问；**不该问时返回 None**（这是本模块最重要的一半）。

    该问的四个条件，缺一即不问：
    1. 硬约束已齐（预算 + 用途 + 人数）——不齐时先问硬的那句，那才是必要提问；
    2. 用户**没强调过任何维度**（`weights` 为空）——已强调就别问「你想要什么」；
    3. 本会话**没问过**（`probed_dims` 为空）——只问一次；
    4. 候选维度确实在封闭枚举里（见 `_pick_dims`）。
    """
    if profile.budget.min is None and profile.budget.max is None:
        return None
    if not profile.usage or profile.passengers is None:
        return None
    if profile.weights:
        return None
    if getattr(profile, "probed_dims", None):
        return None

    dims = _pick_dims(profile)
    if len(dims) < _PROBE_SIZE:
        return None
    return Clarification(
        question="你更看重哪一点？我按这个来排。",
        options=[_DIM_LABELS[d] for d in dims],
        missing=["emphasis"],
    )


def apply_probe_answer(profile: Any, message: str) -> bool:
    """把用户对软缺口追问的回答落成权重。返回是否命中。

    只认**被问过的那两个维度**——用户随口提的其它词仍由 `extract_hints` 的强调词
    路径处理，这里不越权。权重值用 `engine._WEIGHT_RAISE`（同一条生产路径算出来的
    那个数），本地**不留第二份常量**，免得改一处漂一处。
    """
    probed = list(getattr(profile, "probed_dims", None) or [])
    if not probed:
        return False
    low = (message or "").lower()
    for dim in probed:
        label = _DIM_LABELS.get(dim, "")
        if not label or label not in low:
            continue
        from app.agent.engine import _WEIGHT_RAISE, merge_profile

        merge_profile(profile, {
            "weights": {dim: round(float(DEFAULT_WEIGHTS.get(dim, 0.0)) + _WEIGHT_RAISE, 3)}
        })
        return True
    return False


def mark_probed(profile: Any, dims: list[str]) -> None:
    """记下「问过哪两个维度」——只问一次的依据。"""
    profile.probed_dims = list(dims)
