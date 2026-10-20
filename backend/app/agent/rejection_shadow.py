"""拒绝反馈的影子评估（计划 Phase 3「反馈驱动偏好调整（shadow）」）。

## 为什么要有它

用户说「不喜欢这台」时，系统**会话内**已经把这台排除了（`_apply_rejection`）。
下一个自然的动作是把它**跨会话记住**（计划 Phase 2b 的反馈表）。但那条路的**粒度**
有讲究：

- **款型级**（就记住这一台）：安全，但价值有限——记住一台车，下次也就少推一台；
- **品牌级**（上升为「不喜欢这个品牌」）：价值大，但**可能过激**——
  用户讨厌的往往只是那一台的某个配置，不一定是整个品牌。

本模块算的是**品牌级**这条路的**反事实**：如果把这次拒绝上升为品牌排除项，
推荐会怎么变、**会误伤几台**。**只算不改**（影子），结论进日志，供产品拍板。

## 口径

- `before`：本轮真实推荐的 top-N（已受款型级排除影响）。
- `after`：反事实推荐的 top-N（额外排除被拒车所在品牌）。
- **误伤面**（`same_brand_in_topn`）：`before` 里属于该品牌、**且不是被拒那台**的台数——
  它们不是用户拒绝的那台，却会因为「品牌级排除」而消失。

纯计算（不碰 DB、不调模型），因此可以单测，也可以被别的实现复用。
"""
from __future__ import annotations

from typing import Any


def _ids(variants: list[dict]) -> list[int]:
    return [int(v["variant_id"]) for v in variants if v.get("variant_id") is not None]


def brand_exclusion_delta(
    before: list[dict],
    after: list[dict],
    *,
    brand_name: str | None = None,
    rejected_variant_id: int | None = None,
) -> dict[str, Any]:
    """品牌级排除的反事实差集（纯计算）。

    返回：
    - `entered` / `left`：top-N 里进 / 出的款型 id；
    - `same_brand_in_topn`：**误伤面**——before 里属于该品牌、且不是被拒那台的台数；
    - `topn_changed`：推荐是否真的变了（没变 = 这条拒绝信号对排名无影响）。
    """
    before_ids = _ids(before)
    after_ids = _ids(after)
    rejected = int(rejected_variant_id or 0)
    same_brand = [
        v
        for v in before
        if brand_name
        and (v.get("brand_name") or "") == brand_name
        and int(v.get("variant_id") or 0) != rejected
    ]
    return {
        "entered": [i for i in after_ids if i not in before_ids],
        "left": [i for i in before_ids if i not in after_ids],
        "same_brand_in_topn": len(same_brand),
        "topn_changed": before_ids != after_ids,
        "before_size": len(before_ids),
        "after_size": len(after_ids),
    }
