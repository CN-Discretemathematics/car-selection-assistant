"""品牌提及解析（把「只要奔驰」这类自然语言要求解析为库内品牌 id）。

为什么需要（2026-09 用户实测）：
用户第一句就说「必须是奔驰」，但当时的画像里**没有品牌字段**，于是即便收齐了
预算/用途/人数，推荐 SQL 也从不按品牌过滤，最终推了领克、小鹏、大众、林肯——
用户要的核心硬约束被整条链路丢掉了。

实现要点：
- 只匹配库内 `brands.name` 与 `brands.aliases`（不匹配车系名），长名优先，
  避免「一汽-大众」这类长名被「大众」抢先吃掉；
- 识别否定语境（不要/不考虑/除了…）→ 记为排除品牌，而不是正向约束；
- 对**易与日常词混淆**的品牌名（理想/长安/大众/未来）要求出现购车意图词，
  否则不当作品牌约束（「理想预算 20 万」不应解析成品牌「理想」）。
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.catalog.series_index import _GENERIC_BRAND_SUFFIXES, normalize_name
from app.common.models import Brand, VehicleSeries, VehicleVariant

# 否定语境前缀（作用于品牌名之前 4 个字以内）
NEGATION_PREFIXES = ("不要", "不考虑", "不想", "不想要", "不选", "别买", "别要", "除了", "排除", "拒绝")
# 购车意图词（宽松）：用于判断消息是否在谈买车
INTENT_WORDS = (
    "品牌", "买", "购", "选", "看", "要", "只", "必须", "车型", "车", "suv", "mpv", "轿车", "预算",
)
# 强意图词（用于消解「理想/长安/大众」这类日常词歧义）：
# 只认明确的购车动作，不含「预算/要/看/车」等过宽的词，否则「理想的预算 20 万」会误判成品牌「理想」
STRONG_INTENT_WORDS = ("品牌", "买", "购", "选车", "只要", "必须", "就要", "想要", "考虑", "关注", "看看")
# 易与日常词混淆的品牌名（归一化后）：仅在同句出现购买意图词时才作约束
AMBIGUOUS_BRANDS = {"理想", "长安", "大众", "未来", "启辰", "东风"}


def _strip_generic_suffix(brand_name: str) -> str:
    """「小米汽车」→「小米」。口语里没人说全名。"""
    for suffix in _GENERIC_BRAND_SUFFIXES:
        if brand_name.endswith(suffix) and len(brand_name) - len(suffix) >= 2:
            return brand_name[: -len(suffix)]
    return ""

# 品牌名索引缓存（指纹 = 活跃品牌数 + max(id)）；pytest 每次新建库，指纹可能碰撞，故测试下禁用
_cache: dict = {"fingerprint": None, "entries": ()}


def _load_entries(db: Session) -> tuple[tuple[str, int, str], ...]:
    """(归一化品牌名或别名, brand_id, 展示名)，长名优先。

    额外登记**去掉通用后缀**的品牌简称（2026-10-05 生产实测）：
    全库有 5 个品牌名带「汽车」后缀（零跑汽车 / 小米汽车 / 理想汽车 / 吉利汽车 /
    江淮汽车）且 `aliases` **全为空**，而用户口语只说「小米」「吉利」「江淮」。
    实拍后果——同一句「品牌 + 问车型」换个说法就分裂成两条路：

        「小米有几款车」        -> **全库盘点**（908 个车系），完全不提小米
        「小米的车型有哪些」     -> 「暂时没有可核对的小米在售车型资料」
        「奔驰有几款车」        -> 正确：「奔驰在售车型共 56 款」

    品牌名恰好就是简称的（奔驰/特斯拉/比亚迪）不受影响，所以这表现为
    **同一类问题里一部分品牌能问、一部分不能**。

    两条防误伤：
      1. 简称若**恰好等于另一个品牌的全名**（零跑/理想在库里各自另有同名品牌），
         不登记——否则会同时命中两个 brand_id；
      2. `AMBIGUOUS_BRANDS` 里的日常词（理想/长安/大众…）不登记，
         它们已由「须带购车意图词」的既有规则处理。
    """
    rows = db.execute(
        select(Brand.id, Brand.name, Brand.aliases).where(Brand.active_status == "active")
    ).all()
    exact = {normalize_name(name or "") for _bid, name, _al in rows}
    entries: list[tuple[str, int, str]] = []
    for brand_id, name, aliases in rows:
        for candidate in (name, *(aliases or [])):
            normalized = normalize_name(candidate or "")
            if normalized:
                entries.append((normalized, brand_id, name))
        stem = _strip_generic_suffix(name or "")
        if (
            stem
            and stem != normalize_name(name or "")
            and stem not in exact
            and stem not in AMBIGUOUS_BRANDS
        ):
            entries.append((stem, brand_id, name))
    entries.sort(key=lambda item: -len(item[0]))
    return tuple(entries)


def brand_entries(db: Session) -> tuple[tuple[str, int, str], ...]:
    return _load_entries(db)


#: 切段用的字符：归一化消息里，这些字把句子分成「几个比较候选」。
#:
#: ⚠️ 2026-10-06 更正过一次方向。此前刻意**不含**「，」，理由写的是
#: 「汉的油耗怎么样，理想一点吗」里的「理想」前面正是「，」，会误报成品牌。
#: 那是**推演，没实测**。实测：加上逗号后该句切出 `['汉的油耗怎么样', '理想一点吗']`，
#: 剥掉尾巴「吗」后是「理想一点」≠「理想」，**那条误报根本不会发生**；
#: 而代价是实打实的——「预算20万，大众和汉哪个好」「汉，大众哪个好」这类
#: 非常自然的写法在原方案下 **100% 漏报**。于是加回逗号（全半角都加）。
COMPARISON_LINK_CHARS = frozenset("和跟与及或对比，,")

#: 问句尾巴。从每段**末尾**剥掉，直到剥不动为止。
#: 这份清单**不需要完备**——缺一项的后果是「本该报却没报」（漏报），而不是误报。
#: 方向是安全的那一侧：先按连接词切段，品牌词必须与整段**完全相等**才认。
_QUESTION_TAILS = (
    "值得买吗", "值不值", "能买吗", "是不是", "好不好", "怎么样", "哪个好",
    "哪个", "好吗", "有吗", "是吗", "吗", "呢", "吧", "啊", "的", "好", "？", "?",
)


def brand_candidates_in_message(db: Session, message: str) -> set[str]:
    """消息里**独立作为一个比较候选**出现的库内品牌名。

    判据只有一条：**按比较连接词切段、剥掉每段末尾的问句尾巴之后，品牌词与整段
    完全相等**。

    ## 三步，缺一不可

    1. **遮蔽品牌词**（见函数体）。不遮蔽的话切段字符会劈进品牌词：
       「比亚迪」含「比」，「比亚迪和汉哪个好」被切成 `['比','亚迪','汉']`。
    2. 按比较连接词切段（全半角逗号也算）。
    3. 剥掉每段末尾的问句尾巴，要求品牌词与整段**完全相等**。

    ## 为什么不是「重建用户写了什么再建区间」

    2026-10-06 血泪：先前用的是「从库里重推一遍用户可能写的字面，建区间，再看品牌词
    是否落在所有区间之外」。那个方向默认是「是」，只要**多一类字面来源**就会漏进门，
    而 `resolve_series` 的候选来源有**五类**：

        车系名 / 品牌+车系 / 去掉品牌前缀的短名 / 别名 / 在售款型显示名

    连续四轮各堵了一扇门（AMG GT 里的 MG → 北京 → 名爵空品牌行 → C级AMG 裸型号），
    第五扇（在售款型显示名）一直开着——评测语料 q0069
    「帮我对比 2026款 2.0L e:HEV 锐·领享版 和 2026款 AMG GLB 35 4MATIC 的配置差异」
    在那个版本上仍在末尾多报一段「MG 是品牌，库里有 7 款」。

    ## 为什么不是「品牌词紧邻连接词」

    那样试过，默认方向虽然翻过来了，却仍有两类误报：

        「奔驰C级AMG和朗逸哪个好」  AMG|和|朗逸  「MG」紧跟「和」，但在「amg」里面
        「朗逸和北京现代ix35哪个好」  和|北京|现代  「北京」紧跟「和」，但在车系名里面

    中文没有词边界，「和星芒S7」里的「星芒」和「和汉」里的「汉」在结构上**完全一样**，
    光看左右邻字分不开。**整段相等**能分开：前者切出「星芒S7」、后者切出「汉」。

    ## 真实库逐条核对

        「大众和汉哪个好」            → 段 [大众, 汉]        → 大众 ✅
        「汉和大众哪个好」            → 段 [汉, 大众]        → 大众 ✅
        「北京现代ix35和北京哪个好」   → 段 [北京现代ix35, 北京] → 北京 ✅
        「朗逸和北京现代ix35哪个好」   → 段 [朗逸, 北京现代ix35] → 不报 ✅
        「MG和汉哪个好」              → 段 [MG, 汉]          → MG ✅
        「比亚迪和汉哪个好」          → 段 [比亚迪, 汉]      → 比亚迪 ✅（不遮蔽会被切成 [比, 亚迪, 汉]）
        「预算20万，大众和汉哪个好」    → 段 [预算20万, 大众, 汉] → 大众 ✅（逗号也是切段字）
        「五菱和缤果Pro哪个好」        → 段 [五菱, 缤果Pro]    → 五菱（由调用方相减挡掉）
        「AITO问界和汉哪个好」         → 段 [AITO问界, 汉]     → **命中「AITO 问界」这个空品牌行**，
                                              而它旗下 0 款在售 → 最终不报（见调用方「0 款不反问」）
        「奔驰C级AMG值得买吗」         → 段 [奔驰C级AMG]       → 不报 ✅
        「C级AMG值得买吗」             → 段 [C级AMG]           → 不报 ✅
        「AMG GT值得买吗」            → 段 [AMG GT]          → 不报 ✅
        「2025款 熊猫mini 210km 元气熊值得买吗」→ 段 [2025款 熊猫mini…] → 不报 ✅
        「朗逸适合大众家用吗」          → 段 [朗逸适合大众家用]   → 不报 ✅
        「汉的油耗怎么样，理想一点吗」    → 段 [汉的油耗怎么样, 理想一点吗] → 剥「吗」后是
                                              「理想一点」≠「理想」→ 不报 ✅
    """
    normalized = normalize_name(message)
    if not normalized:
        return set()
    entries = _load_entries(db)
    labels: dict[str, str] = {}
    for name, _brand_id, label in entries:
        labels.setdefault(name, label)

    # **先遮蔽品牌词，再切段。** 这是本函数最容易漏掉的一步：
    # 切段字符会劈进品牌词里。「比亚迪」含「比」（「对比」贡献的单字），
    # 于是「比亚迪和汉哪个好」被切成 ['比', '亚迪', '汉']——库里车系最多的品牌之一
    # （34 款在售）**永久不可达**，而且没有任何测试或文档提到它。
    # 遮蔽成不含切段字符的占位符，这一整类冲突就不存在了：
    # 品牌词表与切段字符集共用字母表，而遮蔽让两者不再互相干扰。
    # `_load_entries` 已按长度降序，长名先遮，短名不会被长名内部的碎片顶掉。
    masked = normalized
    marks: list[str] = []
    for name, _brand_id, _label in entries:
        if name in masked:
            marks.append(name)
            masked = masked.replace(name, f"\x00{len(marks) - 1}\x00")
    tokens = {f"\x00{i}\x00": name for i, name in enumerate(marks)}

    found: set[str] = set()
    for segment in re.split(f"[{re.escape(''.join(sorted(COMPARISON_LINK_CHARS)))}]", masked):
        seg = segment.strip()
        while seg:
            for tail in _QUESTION_TAILS:
                if seg.endswith(tail) and len(seg) > len(tail):
                    seg = seg[: -len(tail)]
                    break
            else:
                break
        name = tokens.get(seg)
        if name is not None:
            found.add(labels[name])
    return found


# 明确品牌约束的语气（「只要奔驰」「必须是奔驰」「想买奔驰」）
BRAND_INTENT_RE = re.compile(
    r"(只要|只考虑|只看|只买|必须是|必须|就要|想要|想买|打算买|要买|买个|购买|买|锁定)"
)


def _is_bare_brand(normalized_message: str, normalized_brand: str) -> bool:
    """消息是否基本只有品牌名（用户在回答「哪个品牌」这类追问，如只回「奔驰」）。"""
    remainder = normalized_message.replace(normalized_brand, "")
    return len(remainder) <= 2


def resolve_brand_mentions(
    db: Session,
    message: str,
    *,
    series_names: list[str] | None = None,
    assume_constraint: bool = False,
) -> dict:
    """解析消息中的品牌 → {"brand_ids": [...], "brand_labels": [...], "brand_exclude_ids": [...]}。

    两处消歧（都来自实测回归）：
    1. **品牌名出现在被点名车系的名字里时不算品牌约束**——「银河星愿怎么样」里的「银河」
       是对车系的指代；若当成「只要银河」，下一轮按 银河+燃油 过滤就会得到空结果。
    2. **只有明确约束语气**（只要/必须/想买…）、消息基本只有品牌名、或提问本身就是
       品牌盘点（assume_constraint，如「奔驰都有哪些车型」）时，才升级为硬约束；
       单纯提及（「比亚迪和吉利哪个好」）不写进画像。
    3. **长名优先落实在 span 上**（2026-10-05 实测）：`零跑汽车` 消息会同时命中
       「零跑汽车」与「零跑」两个 brand_id——它们是同一个公司的两个品牌记录，
       约束落在两个 id 上会让后续过滤/画像多带一个无关 id。
    """
    normalized = normalize_name(message)
    if not normalized:
        return {}
    has_intent = any(word in message.lower() for word in INTENT_WORDS)
    has_strong_intent = any(word in message for word in STRONG_INTENT_WORDS)
    series_norms = [normalize_name(name) for name in (series_names or []) if name]
    explicit = assume_constraint or bool(BRAND_INTENT_RE.search(message))
    include: dict[int, str] = {}
    exclude: dict[int, str] = {}
    claimed: list[tuple[int, int]] = []
    for name, brand_id, label in _load_entries(db):
        start = normalized.find(name)
        if start < 0:
            continue
        end = start + len(name)
        if any(start >= cstart and end <= cend for cstart, cend in claimed):
            continue  # 已被更长的品牌名占住（`_load_entries` 已按长度降序）
        if any(name in series_norm for series_norm in series_norms):
            continue  # 品牌名属于被点名的车系名（「银河星愿」）→ 是对车系的指代
        if name in AMBIGUOUS_BRANDS and not (has_intent and has_strong_intent):
            continue  # 「理想的预算 20 万」不当作品牌「理想」；「我想买理想」才算
        if not (explicit or _is_bare_brand(normalized, name)):
            continue  # 只是提及，不构成硬约束
        claimed.append((start, end))
        prefix = normalized[max(0, start - 4): start]
        if any(neg in prefix for neg in NEGATION_PREFIXES):
            exclude[brand_id] = label
        else:
            include[brand_id] = label
    # 同一品牌既被正向提及又被否定时以否定为准（保守：宁可少推不可推错）
    for brand_id in list(include):
        if brand_id in exclude:
            include.pop(brand_id)
    result: dict = {}
    if include:
        result["brand_ids"] = sorted(include)
        result["brand_labels"] = [include[i] for i in sorted(include)]
    if exclude:
        result["brand_exclude_ids"] = sorted(exclude)
    return result


def catalog_overview(
    db: Session,
    *,
    body_types: list[str] | None = None,
    energy_allowed: set[str] | None = None,
) -> dict:
    """全库在售盘点（确定性，供「全部车型有多少款车」这类计数问题）。

    背景（2026-09-17 用户实测）：这句话此前既没进品牌盘点、也没进工具循环，
    直接落进推荐链去追问预算。数量类问题必须读库如实报数。

    口径与站内列表页一致：车系取 `VehicleSeries.active_status == "active"`，
    款型取 `VehicleVariant.status == "on_sale"`；能源分桶语义同 `/vehicles?energy_type=`。

    **能源分桶一律不做减法**（2026-09-17 评审 B3）：`energy_types` 为空的车系既不算燃油、
    也不算新能源，单列 `unlabeled_series_count`。真库实测 23 个在售车系里 20 个未标注，
    「总数 − 燃油」会答出「新能源 22 个」，而按站内口径实际只有 2 个——把缺失值当事实。

    可选过滤（把「SUV 有多少款车」「有多少款新能源车」这类问句答成子集计数，而不是全库数）：
    `body_types` 命中任一即计入；`energy_allowed` 是**已展开的具体能源类型集合**
    （如 BEV/PHEV/HEV/ICE）——泛化词 `new_energy`/`fuel` 由调用方按引擎口径
    （`_expand_energy_prefs`）展开后再传，避免这里再维护一套词表而与推荐链不一致。
    """
    from app.common.enums import NEW_ENERGY_TYPES

    new_energy_set = set(NEW_ENERGY_TYPES)
    rows = db.execute(
        select(
            VehicleSeries.id,
            VehicleSeries.brand_id,
            VehicleSeries.body_type,
            VehicleSeries.energy_types,
            VehicleSeries.source_id,
        )
        # 内连接 Brand：孤儿车系（无品牌/品牌缺失）不计入——口径与站内列表页
        # （vehicles/router.py 的 /vehicles）完全一致；列表页同样不按品牌 active 过滤，
        # 故这里也不过滤，两边数字永远可以对得上（2026-09-17 评审建议 3）。
        .join(Brand, Brand.id == VehicleSeries.brand_id)
        .where(VehicleSeries.active_status == "active")
    ).all()

    wanted_types = set(energy_allowed or ())
    wanted_body = set(body_types or [])

    def keep(row) -> bool:
        if wanted_body and row.body_type not in wanted_body:
            return False
        if not wanted_types:
            return True
        return bool(set(row.energy_types or []) & wanted_types)

    selected = [row for row in rows if keep(row)]
    # 款型数按车系分组一次查完（避免逐车系 N+1；也不用把 1k+ id 塞进 IN 列表）
    variants_per_series = dict(
        db.execute(
            select(VehicleVariant.series_id, func.count(VehicleVariant.id))
            .where(VehicleVariant.status == "on_sale")
            .group_by(VehicleVariant.series_id)
        ).all()
    )
    fuel_count = sum(1 for r in selected if (set(r.energy_types or []) - new_energy_set))
    nev_count = sum(1 for r in selected if (set(r.energy_types or []) & new_energy_set))
    unlabeled = sum(1 for r in selected if not r.energy_types)
    without_variants = sum(1 for r in selected if not variants_per_series.get(r.id))
    # 来源按覆盖车系数排序（同数时按 id 稳定排序，避免引用随扫描顺序抖动）
    source_counts: dict[int, int] = {}
    for row in selected:
        if row.source_id:
            source_counts[row.source_id] = source_counts.get(row.source_id, 0) + 1
    source_ids = [sid for sid, _ in sorted(source_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
    return {
        "series_count": len(selected),
        "variant_count": sum(variants_per_series.get(r.id, 0) for r in selected),
        "brand_count": len({r.brand_id for r in selected if r.brand_id}),
        "fuel_series_count": fuel_count,
        "new_energy_series_count": nev_count,
        "unlabeled_series_count": unlabeled,
        # 燃油与新能源可重叠（同一车系两种款型都有），故不保证 fuel + nev == series_count
        "energy_overlap_count": sum(
            1
            for r in selected
            if (set(r.energy_types or []) - new_energy_set) and (set(r.energy_types or []) & new_energy_set)
        ),
        "without_variants": without_variants,
        "source_ids": source_ids,
    }


def brand_series_overview(db: Session, brand_ids: list[int], budget_max: float | None = None) -> dict:
    """品牌车系概览（确定性，供「奔驰都有哪些车型」这类列举问题）。

    返回：车系总数、按能源类型分布（含燃油 / 仅新能源）、各价格区间车系（可选按预算上限筛），
    以及无价格数据的车系数——让回答能如实说明数据完整度，而不是靠模型记忆。
    """
    from app.catalog import services as catalog
    from app.common.enums import NEW_ENERGY_TYPES

    if not brand_ids:
        return {"brand_names": [], "series_count": 0, "fuel_series_count": 0,
                "new_energy_series_count": 0, "without_price": 0, "series": []}
    brand_names = {
        bid: name
        for bid, name in db.execute(select(Brand.id, Brand.name).where(Brand.id.in_(brand_ids))).all()
    }

    series_rows = db.scalars(
        select(VehicleSeries).where(
            VehicleSeries.brand_id.in_(brand_ids),
            VehicleSeries.active_status == "active",
        )
    ).all()
    items: list[dict] = []
    fuel_count = 0
    for series in series_rows:
        energy = list(series.energy_types or [])
        is_fuel = any(t not in NEW_ENERGY_TYPES for t in energy)
        price_min, price_max = catalog.series_price_range(db, series.id)
        if is_fuel:
            fuel_count += 1
        items.append(
            {
                "series_id": series.id,
                "series_name": series.name,
                "brand_name": brand_names.get(series.brand_id),
                "energy_types": energy,
                "has_fuel": is_fuel,
                "price_min": float(price_min) if price_min is not None else None,
                "price_max": float(price_max) if price_max is not None else None,
                "source_id": series.source_id,
            }
        )
    if budget_max is not None:
        items = [i for i in items if i["price_min"] is not None and i["price_min"] <= budget_max]
    items.sort(key=lambda i: (i["price_min"] is None, i["price_min"] or 0))
    return {
        "brand_names": [brand_names[i] for i in sorted(brand_names)],
        "series_count": len(items),
        "fuel_series_count": fuel_count,
        "new_energy_series_count": len(items) - fuel_count,
        "without_price": sum(1 for i in items if i["price_min"] is None),
        "series": items,
    }
