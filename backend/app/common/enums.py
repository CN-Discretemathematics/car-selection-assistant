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
