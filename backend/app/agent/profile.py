"""画像合并：把一份 hints 并进已有画像。

## 为什么独立成 module

2026-10-20 之前，`merge_profile` 住在 `agent/engine.py` 里。于是：

    engine.py (顶层) ──import──▶ soft_prefs.py
                                      │
                                      └──函数内延迟 import──▶ engine.py

`soft_prefs` 需要 `merge_profile` 来落软偏好，而 `engine` 顶层 import `soft_prefs`
（`engine.respond()` 里那一行 `await soft_prefs.run_if_enabled(...)`）——
**真实的循环依赖**。Python 不允许顶层互相 import，所以 `soft_prefs` 只能把
import 写进函数体。那处延迟 import 是**疤痕**，不是设计。

把 `merge_profile` 移到本 module 后：

    engine.py     ──import──▶ profile.py
    soft_prefs.py ──import──▶ profile.py
    soft_probe.py ──import──▶ profile.py

**环消失**，下游不再需要函数内延迟 import。

## 它是唯一一处画像合并

全仓**只有这一个**画像合并器。软偏好（L1 抽取的）也要走它——不新建第二个合并器，
因为两套口径必然漂移（仓库反复记录过的教训：早先的手写版把「空间优先」算成 0.20，
正则路径算成 0.30，同一句话两种结果）。

本 module **不 import 任何 app.agent.* 模块**（只依赖 `schemas` 与 `weights`），
它是叶子，不制造新环。
"""
from __future__ import annotations

from app.agent.schemas import Budget, UserProfile
from app.agent.weights import DEFAULT_WEIGHTS, WEIGHT_CEILING


def merge_profile(profile: UserProfile, hints: dict) -> UserProfile:
    """把 hints 并进 profile（**原地修改并返回同一个对象**）。

    语义要点：

    - **品牌硬约束并入而非覆盖**——会话内多次提及取并集；**否定项优先**：
      说了「不要奔驰」就不再把它当正向约束。
    - **排除品牌的可读名同样累积**：此前只有 ids，追问回执**结构上就写不出
      被排除的是哪个品牌**，于是用户说过的「别推荐比亚迪」在回执里凭空消失。
    - **权重线索跨轮累加**（如第一轮「空间优先」0.30，后来说「再看重动力」
      动力也 0.30），上限 `WEIGHT_CEILING`：重复强调同一维度会线性累加，
      没有上限时一句车可以叠到 3.0，那等于「只看这一个维度」，
      与综合推荐的产品意图相悖（2026-10-03）。
    """
    if hints.get("budget"):
        profile.budget = Budget(**hints["budget"])
    if hints.get("passengers") is not None:
        profile.passengers = hints["passengers"]
    if hints.get("body_type"):
        profile.body_type = hints["body_type"]
    if hints.get("energy_preference"):
        profile.energy_preference = hints["energy_preference"]
    # 品牌硬约束（用户明确「只要奔驰」等）：并入而非覆盖——会话内多次提及取并集；
    # 否定项优先：说了「不要奔驰」就不再把它当正向约束
    if hints.get("brand_ids") or hints.get("brand_exclude_ids"):
        # `strict=False`：与搬来之前逐字相同的行为（zip 按最短截断）。
        # 品牌 id 与其可读名**本应等长**，但画像可能来自旧版本或被外部写坏过；
        # 此时静默截断好过抛异常把整轮对话带崩。
        labels = dict(zip(profile.brand_ids, profile.brand_labels, strict=False))
        labels.update(
            dict(zip(hints.get("brand_ids") or [], hints.get("brand_labels") or [], strict=False))
        )
        excluded = set(profile.brand_exclude_ids) | set(hints.get("brand_exclude_ids") or [])
        # 排除项的可读名同样要累积（2026-10-05）：此前只有 ids，追问回执**结构上就
        # 写不出被排除的是哪个品牌**，于是用户说过的「别推荐比亚迪」在回执里凭空消失。
        excl_labels = dict(zip(profile.brand_exclude_ids, profile.brand_exclude_labels, strict=False))
        excl_labels.update(
            dict(zip(hints.get("brand_exclude_ids") or [],
                     hints.get("brand_exclude_labels") or [], strict=False))
        )
        kept_ids = (set(profile.brand_ids) | set(hints.get("brand_ids") or [])) - excluded
        profile.brand_ids = sorted(kept_ids)
        profile.brand_exclude_ids = sorted(excluded)
        profile.brand_labels = [labels.get(bid, str(bid)) for bid in profile.brand_ids]
        profile.brand_exclude_labels = [excl_labels.get(bid, str(bid)) for bid in excluded]
    if hints.get("usage"):
        profile.usage = hints["usage"]
    if hints.get("charging_tolerance") is not None:
        profile.charging_tolerance = hints["charging_tolerance"]
    if hints.get("avoid"):
        profile.avoid = sorted(set(profile.avoid) | set(hints["avoid"]))
    # 权重线索跨轮累加（如第一轮「空间优先」0.30，后来说「再看重动力」动力也 0.30）。
    # 上限 WEIGHT_CEILING：重复强调同一维度会线性累加，没有上限时一句车可以叠到 3.0，
    # 那等于「只看这一个维度」，与综合推荐的产品意图相悖（2026-10-03）。
    if hints.get("weights"):
        for dim, w in hints["weights"].items():
            merged = profile.weights.get(dim, 0.0) + float(w)
            profile.weights[dim] = round(min(merged, WEIGHT_CEILING), 3)
        # 首要维度（2026-10-21，计划 §1.2）：取「这一轮被用户抬高」的维度里权重最高的。
        #
        # ⚠️ **不是**「默认权重最高的维度」——默认里 budget 最高（0.30），那是评分口径，
        # 不是用户的偏好。判据必须是「被抬高」（> 默认值）。
        #
        # ⚠️ **只影响文案组织，不动打分与排序**（计划原文的硬约束）：
        # 这里只写画像的一个字段，排序仍由 8 维软评分决定。
        raised = [
            dim
            for dim, w in hints["weights"].items()
            if float(w) > DEFAULT_WEIGHTS.get(dim, 0.0) + 1e-9
        ]
        if raised:
            profile.top_dim = max(raised, key=lambda d: float(hints["weights"][d]))
    return profile
