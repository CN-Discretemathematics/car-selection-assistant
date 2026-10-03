"""领域枚举与常量（唯一权威定义，数据模型与接口共用）。"""
from __future__ import annotations

# 品牌类别
BRAND_TYPES = (
    "domestic_nev",
    "luxury",
    "japanese",
    "american",
    "german",
    "other_fuel",
)

# 能源类型
ENERGY_TYPES = ("BEV", "PHEV", "EREV", "HEV", "ICE")

# 新能源侧（绿牌）：首页「新能源/燃油」二分筛选规则，HEV 归燃油侧
NEW_ENERGY_TYPES = ("BEV", "PHEV", "EREV")

# 车身类型（pickup = 皮卡，属家用乘用车范围；轻客/微卡/微面等商用车不入枚举，
# 保持 NULL 以如实标注「官方资料未披露」而非强行归类）
BODY_TYPES = ("sedan", "suv", "mpv", "pickup")

# 销量口径（portal = 门户榜单口径，如汽车之家销量榜，
# 无法归类为零售/批发/上险时如实标注，不冒充统一口径）
SALES_TYPES = ("retail", "wholesale", "insurance", "portal")

# 续航/油耗工况
CYCLES = ("CLTC", "NEDC", "WLTC")

# 在售状态
ACTIVE_STATUSES = ("active", "inactive")
VARIANT_STATUSES = ("on_sale", "off_sale")
MODEL_YEAR_STATUSES = ("on_sale", "pre_sale", "discontinued")

# 价格类型：只允许官方指导价
PRICE_TYPES = ("official_msrp",)

# 来源类型
SOURCE_TYPES = (
    "official_site",   # 品牌官网/官方车型页
    "official_doc",    # 官方配置表、手册、PDF
    "licensed_data",   # 具备授权的数据服务
    "industry_data",   # 公开可信行业数据
    "user_review",     # 用户评论（仅体验性描述）
    "other",
)

VERIFICATION_STATUSES = ("unverified", "verified", "conflict")
CREDIBILITY_LEVELS = ("high", "medium", "low")

# 缺失数据统一文案（不得显示 0 或由模型补全）
MISSING_VALUE_LABEL = "官方资料未披露"

# 对比规模上限（默认最多比较 3～5 个 SKU）
COMPARISON_MAX_VARIANTS = 5


# ── 能源偏好泛化（2026-10-02 收敛到唯一实现）──────────────────────────────
# 用户说的是「新能源 / 燃油」这类**大类**，而库内是 BEV/PHEV/EREV/HEV/ICE 五个
# 具体类型。泛化规则此前在 agent/engine.py 与 agent/tools.py 各写了一份
# （2026-10-02 审计：两份逐字相同，engine 内部又另有一份 _expand_* 包装）。
# 规则本身是**推荐口径**——改它等于改产品行为，因此集中到本文件，
# 任何调用方都不得再自行展开。
#
# 口径：HEV 归燃油侧（见 NEW_ENERGY_TYPES 注释），即
#   new_energy → {BEV, PHEV, EREV}
#   fuel      → {HEV, ICE}
_GENERIC_ENERGY_ALIASES = ("new_energy", "fuel")


def expand_energy_prefs(prefs: list[str] | tuple[str, ...] | None) -> set[str]:
    """把能源偏好里的 `new_energy` / `fuel` 大类展开为具体能源类型。

    返回可直接用于 SQL `IN (...)` 的集合；**已按 ENERGY_TYPES 全集求并**，
    调用方无需再自行补齐具体类型。
    """
    items = list(prefs or [])
    allowed = {e for e in items if e not in _GENERIC_ENERGY_ALIASES}
    if "new_energy" in items:
        allowed |= set(NEW_ENERGY_TYPES)
    if "fuel" in items:
        allowed |= {t for t in ENERGY_TYPES if t not in NEW_ENERGY_TYPES}
    return allowed


def expand_avoid(avoid: list[str] | tuple[str, ...] | None) -> tuple[set[str], set[str]]:
    """把排除项展开为 (能源类型集合, 车身类型集合)，用于 SQL `NOT IN (...)`。

    泛化口径与 `expand_energy_prefs` **必须一致**——两处曾各自实现，
    长期可能漂移；现在共用同一实现，杜绝「偏好说排除燃油、排除项却没排除」的偏差。
    """
    items = set(avoid or [])
    energy_avoid = {e for e in items if e in ENERGY_TYPES}
    if "new_energy" in items:
        energy_avoid |= set(NEW_ENERGY_TYPES)
    if "fuel" in items:
        energy_avoid |= {t for t in ENERGY_TYPES if t not in NEW_ENERGY_TYPES}
    body_avoid = {b for b in items if b in BODY_TYPES}
    return energy_avoid, body_avoid
