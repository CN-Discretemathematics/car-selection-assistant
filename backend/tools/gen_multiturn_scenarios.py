"""生成多轮场景题库（首批 150 题 = 3 场景 × 50；第二批 +1 场景 × 50）。

## 为什么用生成器而不是手写 JSON

150 道题若手写，变量分布（品牌 / 价位段 / 车身 / 能源 / 拒绝原因）
必然失衡——50 道题可能只是 5 个场景 × 10 个变体，统计意义≈5。
生成器保证**每题的组合都不同**，且关键变量**分层覆盖**。

这与单轮题库的生成器（`tools/gen_eval_questions.py`）是同一套思路：
分层抽样 + 固定种子（可复现）+ 分布记录。

## 四类场景

1. `reject_then_no_recommend`（50）—— 拒过的那台，下一轮还推不推？
   **同会话**，当前系统可测。
2. `returning_user_memory`（50）—— 回访用户的记忆是否生效？
   **跨会话**，依赖 Phase 1 的持久化身份与偏好记忆 → 默认标 `enabled: false`。
3. `memory_deleted`（50）—— 删掉的记忆是否真的不生效？
   **跨会话**，同上。
4. `rejection_remembered_across_sessions`（50）—— 拒绝**跨会话**还记得吗？
   **跨会话**，依赖 Phase 2b 的反馈表（拒绝写库 + 新会话读回）→ 默认 `enabled: false`。

## 跨会话场景默认关，用开关打开

跨会话场景要跑，前提是**跨会话**真的跨了：引擎 `handle` 支持 `identity` ✓
且 `DURABLE_PREFS_MODE=on`（`off`/`shadow` 模式**不回填**记忆、**不读写**反馈，
断言必然红）。所以默认 `enabled: false`，用 `--enable-memory-scenarios` 打开。

评测框架默认**只跑 enabled 的场景**（未启用的单独报数、不计入分母）——
否则「通过率」会被未落地的能力拉低，看报告的人会误判成「功能坏了」，
更糟的是有人会去**改断言**让它变绿。

## 需求句必须**可满足**（2026-10-21 修正）

旧版生成器用**硬编码**的品牌 / 车身 / 能源 / 价位组合拼需求句，两个后果：

1. 需求句**缺「用途」**——那是引擎的必需项（`next_clarification` 会问「主要用途是什么？」），
   于是引擎**从不推荐**、只反问 ⇒ 断言恒真。实测：旧生成器的 50 道题全部被判
   「无推荐→断言恒真」，**一道都没真跑起来**；
2. 硬编码组合**库里未必有这种车**（如「埃安的轿车」——埃安没有轿车）⇒ 同样出不了推荐。

⇒ 改为**从库里抽真实车系**（与单轮题库生成器 `gen_eval_questions.py` 同一范式）：
题面里的品牌 / 车身 / 能源 / 价位 / 用途**都从那台车派生**，
需求句**按构造即可满足** ✓，且被拒的那台**就是需求里那台**（断言不是恒真）。

## 锚点用字符串

单轮题库的 410 道题里 406 道的 `anchors.series_id` 指向的车与题面说的不是同一台
（库重导入后 id 重排，2026-10-20 实测）。多轮题库**只用车系名**做锚点。

用法：
    python tools/gen_multiturn_scenarios.py --out eval/multiturn.json
    python tools/gen_multiturn_scenarios.py --out eval/multiturn.json --enable-memory-scenarios
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path)
from sqlalchemy import func, select  # noqa: E402

from app.common.database import get_session_factory  # noqa: E402
from app.common.models import Brand, OfficialPrice, VehicleSeries, VehicleVariant  # noqa: E402

# ── 变量表 ──────────────────────────────────────────────────────────────────
#
# **不硬编码库内 id**——只用名字，题库在任何库上都能跑；
# 但**品牌 / 车身 / 能源 / 用途 / 价位从库里抽**（见 `sample_requirements`），
# 否则需求句可能库里根本满足不了（实测：旧生成器 50 道题全部「无推荐→断言恒真」）。

PASSENGERS = [3, 4, 5]

#: 拒绝理由 → 用户会怎么说。措辞取自真实口语，不做成语。
REJECT_PHRASES = [
    "太贵了",
    "太大了",
    "空间不够",
    "不喜欢这个外观",
    "想要个更省油的",
    "预算超了",
    "这个品牌我不想要",
]

# ── 从库里抽真实车系（需求句必须**可满足**）────────────────────────────────
#: 用途 → 与之**相容**的车身。引擎把用途下推成车身约束
#: （通勤→轿车；家庭/长途→SUV/MPV，见 `tools.py` 的 usage 过滤），
#: 用途与车身不相容 ⇒ 候选集恒空 ⇒ 又是「无推荐→断言恒真」。
USAGE_BY_BODY: dict[str, tuple[str, ...]] = {
    "sedan": ("通勤",),
    "suv": ("家用", "长途自驾"),
    "mpv": ("家用", "长途自驾"),
}

#: 能源枚举 → 题面用词（引擎 `engine._ENERGY_HINTS` 的键）。
ENERGY_WORDS: dict[str, str] = {
    "BEV": "纯电",
    "PHEV": "插混",
    "EREV": "增程",
    "HEV": "油混",
    "ICE": "燃油",
}

#: 车身枚举 → 题面用词（引擎 `engine._BODY_HINTS` 的键，匹配时统一小写）。
BODY_WORDS: dict[str, str] = {"sedan": "轿车", "suv": "SUV", "mpv": "MPV"}


#: 校验「追问后还有新的一批」时用的拒绝理由。
#:
#: 为什么固定一个：拒绝理由若夹带会改约束的词（预算/能源/品牌），引擎会顺着改，
#: 候选池随之变化 ⇒ 校验结果与题库实际用的理由不一致。
#: 实测（2026-10-21）：8 个理由的「第三轮那台车回来率」**完全一样**（13/20）
#: ⇒ 理由不是变量，池子大小才是。
_VERIFY_REJECT_REASON = "空间不够"


def _band_for(price: float) -> tuple[int, int]:
    """该价格所在的预算档（万元）：floor 到 ceil，两侧各放宽 2 万。

    为什么**窄档会把断言压成恒真**：档宽时（如 8-12 万）同价位的兄弟车太多，
    被抽中的那台可能压根不在 top-5 ⇒「它不该再被推荐」**恒真**
    （实测：宽档下 43/50 通过、7 条恒真——那 7 条正是「被拒的车是同品牌的兄弟，没进榜」）。

    为什么**不是 1 万窄档**（2026-10-21 修正）：窄档把候选压到个位数
    （实测候选池**中位数 3**，44/50 的需求**没有下一批**可推）⇒
    「再推荐几款」之后候选集已空 ⇒ 拒绝场景的第三轮**推不出任何车**
    ⇒ 断言无对象可验（被按轮守卫判无效）。
    ⇒ 两侧各放宽 2 万后候选池**中位数 13**，84% 的需求留得下下一批 ✓。
    窄到个位数与宽到几十台之间没有别的档：**再窄**验不了「被拒的不再推」，
    **再宽**验不了「被拒的那台在榜里」，这个宽度是两头都要的平衡点。
    """
    wan = price / 10000
    lo = int(wan)
    hi = lo if abs(wan - lo) < 1e-6 else lo + 1
    return (max(1, lo - 2), hi + 2)


def _recommended_series_brands(db, turn: str) -> list[tuple[str, str]]:
    """跑一次**真实推荐**，返回 (车系名, 品牌名) 列表。

    为什么要**品牌**也带出来：排除项是**品牌级**的（用户说「不要丰田」⇒ 该品牌全部车系出局），
    而库里一个品牌往往有多个车系 ⇒ 只看车系名判不出「这个品牌还有没有车在榜」。
    """
    import asyncio

    from app.agent.engine import get_agent_engine
    from app.agent.session import get_session_store

    engine = get_agent_engine()
    store = get_session_store()
    out = asyncio.run(engine.respond(db, store.create(), turn))
    return [
        (str(v.series_name), str(getattr(v, "brand_name", "") or ""))
        for v in out.recommended_variants or []
    ]


def _recommended_series(db, turn: str) -> list[str]:
    """跑一次**真实推荐**，返回被推荐的车系名。

    用来确认「被拒的那台」确实在榜里——否则「它不该再被推荐」是**恒真**的
    （实测：宽预算档下 7/50 恒真，窄档下仍有 5/50）。
    走引擎的真实路径（`respond`），不自己复现一遍筛选逻辑（复现＝会漂）。
    """
    return [name for name, _brand in _recommended_series_brands(db, turn)]


def _price_bucket(band: tuple[int, int]) -> int:
    """预算档归到**5 万一档**的价位桶（与 `audit_multiturn_corpus` 同一口径 ✓）。

    用桶而不是原始档：原始档有 20 种（每种 1-6 题）⇒ 统计意义≈1；
    桶有 8 种 ⇒ 每桶能分到 6 题左右 ✓。
    """
    return (int(band[0]) // 5) * 5


def _stratified_order(pool: list[dict], rng: random.Random) -> list[dict]:
    """按（能源 × 价位桶）**两层轮转**排序候选。

    ## 为什么不能均匀抽样

    实测（2026-10-21，分支 24 的审计工具）：均匀抽样出来的题库
    **燃油 41 / 纯电 8 / 插混 1 / 增程 0**，价位桶也集中在两个（5-10 万 21、
    10-15 万 15 = 72%）⇒ 能源与价位这两个变量**几乎没有覆盖**
    ⇒ 50 题在这些维度上的统计意义≈5（计划 §七「50 题/场景足以判定」的怀疑成真）。

    ## 两层轮转

    1. **能源**之间轮转 ⇒ 四种能源各占约 1/4；
    2. 每个能源内部再按**价位桶**轮转 ⇒ 每个（能源 × 桶）格都有机会被抽到。

    抽不满时（某些格没有候选）自动退化成「有多少给多少」✓。
    """
    by_energy: dict[str, list[dict]] = {}
    for cand in pool:
        by_energy.setdefault(cand["_energy"], []).append(cand)

    # 每个能源内部按价位桶轮转 → 每条序列
    seqs: list[list[dict]] = []
    for _energy, items in sorted(by_energy.items()):
        by_bucket: dict[int, list[dict]] = {}
        for cand in items:
            by_bucket.setdefault(cand["_bucket"], []).append(cand)
        for bucket_items in by_bucket.values():
            rng.shuffle(bucket_items)
        buckets = sorted(by_bucket)
        pending = {b: list(by_bucket[b]) for b in buckets}
        seq: list[dict] = []
        while any(pending.values()):
            for b in buckets:
                if pending[b]:
                    seq.append(pending[b].pop())
        seqs.append(seq)

    # 能源之间轮转
    ordered: list[dict] = []
    for i in range(max((len(s) for s in seqs), default=0)):
        for seq in seqs:
            if i < len(seq):
                ordered.append(seq[i])
    return ordered


def sample_requirements(db, rng: random.Random, count: int) -> list[dict]:
    """从库里抽 count 个**可满足、且被拒那台真会被推荐**的需求。

    每项含：车系名（被拒的那台）、品牌、车身、能源、用途、预算档、人数。

    ⚠️ 逐个候选**验一遍**（跑真实推荐）——同品牌的兄弟车会把被抽中的那台挤出
    top-5，断言于是恒真（实测 5/50）。验过才收 ⇒ 50 道题每一道都在验行为。
    """
    rows = db.execute(
        select(
            VehicleSeries.name,
            VehicleSeries.body_type,
            VehicleSeries.energy_types,
            Brand.name,
            func.min(OfficialPrice.price_cny),
            func.max(OfficialPrice.price_cny),
        )
        .join(Brand, Brand.id == VehicleSeries.brand_id)
        .join(VehicleVariant, VehicleVariant.series_id == VehicleSeries.id)
        .join(OfficialPrice, OfficialPrice.variant_id == VehicleVariant.id)
        .where(
            VehicleSeries.active_status == "active",
            Brand.active_status == "active",
            VehicleVariant.status == "on_sale",
            OfficialPrice.effective_to.is_(None),
            OfficialPrice.price_type == "official_msrp",
        )
        .group_by(
            VehicleSeries.id,
            VehicleSeries.name,
            VehicleSeries.body_type,
            VehicleSeries.energy_types,
            Brand.name,
        )
    ).all()

    pool: list[dict] = []
    for name, body, energies, brand, pmin, _pmax in rows:
        if body not in USAGE_BY_BODY or pmin is None:
            continue
        words = [ENERGY_WORDS[e] for e in (energies or []) if e in ENERGY_WORDS]
        if not words:
            continue
        energy_cn = rng.choice(words)
        band = _band_for(float(pmin))
        pool.append(
            {
                "series_name": name,
                "brand": brand,
                "body_cn": BODY_WORDS[body],
                "energy_cn": energy_cn,
                "usage_cn": rng.choice(USAGE_BY_BODY[body]),
                "band": band,
                "passengers": rng.choice(PASSENGERS),
                # 分层的两个键（见 `_stratified_order`）
                "_energy": energy_cn,
                "_bucket": _price_bucket(band),
            }
        )

    kept: list[dict] = []
    for cand in _stratified_order(pool, rng):
        if len(kept) >= count:
            break
        try:
            recommended = _recommended_series(db, _recommend_turn(cand))
        except Exception:  # noqa: BLE001 —— 单个候选验不过不该让整批失败
            continue
        if cand["series_name"] not in recommended:
            continue
        # 只收**第一轮推得出 ≥2 个车系**的需求。
        #
        # ⚠️ 为什么（2026-10-21 实测）：第一轮只推得出**一台**车时，
        # 拒绝之后候选集就空了 ⇒ 第三轮「被拒那台不在榜」是**白送的**
        # （推荐列表空 ⇒ 那台车当然不在）⇒ 场景通过但什么都没验。
        # 实测：单候选需求下，拒绝功能**整个缺失**的分支仍有 17/50「碰巧通过」。
        # 多候选时，拒绝之后**还有别的可推** ⇒ 断言才真的在验「被拒的那台被拿掉了」。
        if len({n for n in recommended if n}) < 2:
            continue
        cand["first_turn_series"] = sorted({n for n in recommended if n})
        # 再验一次：追问「再推荐几款」之后**必须还有新的一批**（2026-10-21）。
        #
        # 引擎现在会把**已经展示过的车系**排掉 ⇒ 候选池不够大时，第三轮**推不出车**，
        # 第三轮的断言就没有对象可验（实测：池子中位数 3 时拒绝场景 49 → 4）。
        # ⇒ 只收「追问后还有新的一批」的需求。
        #
        # ⚠️ 这条校验**依赖引擎的「排除已展示」能力**：没有该能力时（Phase 1 之前）
        # 追问会推回同一批，校验会把**所有**候选都判掉 ⇒ 探测能力、缺能力则跳过。
        if _engine_supports_shown_exclusion() and not _reask_yields_new_cars(
            db, cand, recommended
        ):
            continue
        kept.append(cand)
    return kept


def _engine_supports_shown_exclusion() -> bool:
    """引擎是否支持「排除已展示过的车系」（画像里有 `shown_series_ids`）。"""
    from app.agent.schemas import UserProfile

    return "shown_series_ids" in UserProfile.model_fields


def _reask_yields_new_cars(db, req: dict, first_turn: list[str]) -> bool:
    """追问「再推荐几款」之后是否**还有新的一批**车可推。

    跑**真实引擎**，且照搬题库的三轮时序：需求 → 拒绝 → 追问。
    ⚠️ 必须带上「拒绝」那一轮：拒绝轮也会展示一批车，
    而「已展示过的车系」是**累积**的 ⇒ 两轮累计起来的排除量比一轮大
    （实测：只按两轮校验时，第三轮仍有 15/50 推不出车）。

    引擎不支持拒绝时（Phase 1 之前）跳过拒绝轮，按两轮校验。
    """
    import asyncio

    from app.agent.engine import get_agent_engine
    from app.agent.session import get_session_store

    engine = get_agent_engine()
    store = get_session_store()
    sid = store.create()
    out1 = asyncio.run(engine.respond(db, sid, _recommend_turn(req)))
    names1 = {v.series_name for v in out1.recommended_variants or []}
    if req["series_name"] not in names1:
        return False
    if _engine_supports_rejection():
        asyncio.run(
            engine.respond(db, sid, f"{req['series_name']}不要，{_VERIFY_REJECT_REASON}")
        )
    out_last = asyncio.run(engine.respond(db, sid, "再推荐几款"))
    names_last = {v.series_name for v in out_last.recommended_variants or []}
    return bool(names_last) and not (names1 & names_last)


def _engine_supports_rejection() -> bool:
    """引擎是否支持「拒绝后不再推」（画像里有 `rejected_variant_ids`）。"""
    from app.agent.schemas import UserProfile

    return "rejected_variant_ids" in UserProfile.model_fields


def _recommend_turn(req: dict) -> str:
    """需求句：品牌 / 车身 / 能源 / 用途 / 预算 / 人数**全部来自那台真实车系**。

    ⚠️ 品牌必须写成**硬约束**（「只要X的…」）：写成「X的…」时品牌只是偏好，
    同价位的兄弟车照样进 top-5，被拒的那台可能压根没被推荐过 ⇒
    「它不该再被推荐」恒真（实测 39/50 恒真，只有 11/50 真验到了）。
    """
    lo_w, hi_w = req["band"]
    return (
        f"预算{lo_w}到{hi_w}万，只要{req['brand']}的{req['body_cn']}，{req['energy_cn']}的，"
        f"{req['usage_cn']}为主，家里{req['passengers']}口人用"
    )

#: 「再推荐」的各种说法。
#:
#: ⚠️ 只收**会触发重推**的问法（2026-10-21 实测，合流分支 17+8）：
#:
#: | 问法 | 第三轮有没有结构化推荐 |
#: |---|---|
#: | `再推荐几款` | 8/9 有 ✓ |
#: | `换个推荐` | 11/11 有 ✓ |
#: | `继续推荐` | 8/8 有 ✓ |
#: | `还有别的吗` | **0/11** ✗ |
#: | `再看看其他的` | **0/11** ✗ |
#:
#: 后两种被引擎判成**闲聊**（`is_chatty`）⇒ 走聊天分支、回一句模板，
#: **不重推** ⇒ 第三轮「被拒那台不在榜」是白送的（结构化推荐列表为空）。
#: 框架的按轮守卫会把这类场景判成 INVALID（不假通过），
#: 但为了题库**有效覆盖**，这里只保留会重推的问法。
#:
#: ⚠️ 「还有别的吗」不重推本身是个**产品缺陷候选**（用户明确要更多车，
#: 引擎却反问预算）——待立项，不在本题库的范围里。
REASK_PHRASES = [
    "再推荐几款",
    "换个推荐",
    "继续推荐",
]

#: 被拒的那台车**就是需求里抽到的那台**（`sample_requirements`）⇒ 断言不恒真：
#: 旧版从硬编码列表里随机挑，可能挑一台压根不在推荐列表里的车，
#: 于是「它不该再被推荐」这条断言**恒真**（通过但什么都没验）。


def gen_reject_scenarios(rng: random.Random, requirements: list[dict]) -> list[dict]:
    """场景 1：拒过的那台，下一轮还推不推？"""
    out = []
    for i, req in enumerate(requirements):
        reason = rng.choice(REJECT_PHRASES)
        reask = rng.choice(REASK_PHRASES)
        car = req["series_name"]

        turns = [
            {"user": _recommend_turn(req), "assert": []},
            # 拒绝**当轮**就该生效：本题面是**点名式**拒绝（「甲车系不要」），
            # 排除的是整个车系 ⇒ 它不该再出现在本轮推荐里。
            #
            # ⚠️ 这条断言是**严格性**的来源：没有它，拒绝功能整个缺失时
            # 仍有 23/50「碰巧通过」——那台车只是掉出了榜单，不是被拒了。
            # 那种通过**验不到任何东西**（实测：分支 7 单独跑 23/50，
            # 加上这条断言后 0/50）。
            {
                "user": f"{car}不要，{reason}",
                "assert": [{"type": "not_recommended", "car": car}],
            },
            {
                "user": reask,
                "assert": [{"type": "not_recommended", "car": car}],
            },
        ]
        out.append(
            {
                "id": f"mt_reject_{i + 1:04d}",
                "scenario": "reject_then_no_recommend",
                "enabled": True,
                "turns": turns,
            }
        )
    return out


def gen_returning_scenarios(
    rng: random.Random, requirements: list[dict], *, enabled: bool = False
) -> list[dict]:
    """场景 2：回访用户的记忆是否生效？（跨会话，依赖 Phase 1）"""
    out = []
    for i, req in enumerate(requirements):
        lo, hi = req["band"]
        lo, hi = lo * 10000, hi * 10000

        out.append(
            {
                "id": f"mt_return_{i + 1:04d}",
                "scenario": "returning_user_memory",
                # 跨会话：需要 Phase 1 的持久化身份与偏好记忆，尚未实现 → 先不启用
                "enabled": enabled,
                "reason_disabled": "依赖 Phase 1：持久化身份 + 偏好记忆（尚未实现）",
                "turns": [
                    {
                        "user": _recommend_turn(req),
                        "assert": [],
                        "session": "first",
                    },
                    {
                        # 新会话：回访用户的画像应被自动带回
                        "user": "按我之前说的条件推荐",
                        "assert": [
                            # 查**结构化口径**（response.filters 的预算区间），
                            # 不查正文裸字符串——正文里的预算必然与车价混在一起
                            # （12-18 万档有 12.99 万的车），裸字符串会双向误判
                            # （实测：删记忆场景 50/50 条断言会误伤正确答案）
                            {"type": "budget_in", "min": lo, "max": hi},
                        ],
                        "session": "second",
                    },
                ],
            }
        )
    return out


def gen_memory_deleted_scenarios(
    rng: random.Random, requirements: list[dict], *, enabled: bool = False
) -> list[dict]:
    """场景 3：删掉的记忆是否真的不生效？（跨会话，依赖 Phase 1）

    ## 必须带**阳性对照**轮（2026-10-21 实测后补）

    只断言「删完不生效」有个致命漏洞：**分不清「删掉了」和「本来就没记住」**。
    实测（`DURABLE_PREFS_MODE=off`，记忆功能整体关闭）：本场景**照样 50/50 通过**
    ——它验的是「本来就没有」，不是「删掉了」。这种「通过」是**恒真**。

    所以删除**之前**先加一轮跨会话的阳性对照：第二个会话里先断言「记忆还在」
    （`budget_in`），删完再到第三个会话断言「记忆没了」（`budget_not_in`）。
    记忆功能整体关闭时，对照轮必红 ⇒ 本场景**不可能假通过**。

    对照轮放在**第二个会话**、删除轮也在第二个会话（删的是**跨会话记忆**，
    会话画像不动——与设置页撤回同一语义），断言轮在**第三个会话**。
    """
    out = []
    for i, req in enumerate(requirements):
        lo, hi = req["band"]
        lo, hi = lo * 10000, hi * 10000

        out.append(
            {
                "id": f"mt_delete_{i + 1:04d}",
                "scenario": "memory_deleted",
                "enabled": enabled,
                "reason_disabled": "依赖 Phase 1：持久化身份 + 记忆删除接口（尚未实现）",
                "turns": [
                    {
                        "user": _recommend_turn(req),
                        "assert": [],
                        "session": "first",
                    },
                    {
                        # 阳性对照：换会话，记忆应**仍然生效**。
                        # 没有这一轮，本场景就分不清「删掉了」和「本来就没记住」
                        # （实测：记忆功能整体关闭时照样 50/50 通过 ⇒ 恒真）。
                        "user": "按我之前说的条件推荐",
                        "assert": [{"type": "budget_in", "min": lo, "max": hi}],
                        "session": "second",
                    },
                    {"user": "把之前记的预算忘掉", "assert": [], "session": "second"},
                    {
                        # 删掉预算后，**换个会话**再看：记忆不应再回填
                        "user": "现在推荐一台车",
                        "assert": [
                            # 查**结构化口径**（response.filters 的预算区间）：
                            # 裸字符串 `not_mentions '12'` 会被合法车价误伤
                            # （12-18 万档有 12.99 万的车 ⇒ 答案里的「12」判成失败），
                            # 实测 50/50 条断言全中这一枪 ⇒ 假否定 ⇒ 低估能力
                            {"type": "budget_not_in", "min": lo, "max": hi},
                        ],
                        "session": "third",
                    },
                ],
            }
        )
    return out


def gen_rejection_remembered_scenarios(
    rng: random.Random, requirements: list[dict], *, enabled: bool = False
) -> list[dict]:
    """场景 4：拒绝**跨会话**记住（Phase 2b「反馈表」的验收题）。

    会话 1 说「甲车系不要」⇒ **新会话**（同一身份）再问同一需求 ⇒ 那台车**不再出现**。

    ## 为什么要有这一类

    会话内拒绝（`rejected_variant_ids`）本来就生效 ⇒ 场景 1 已经验过 ✓。
    真正的新东西是**跨会话**：「拒绝」被写进反馈表、下次会话读回 ⇒
    少这一类题，Phase 2b 就只有单测、没有**评测级**的验收 ✓。

    ## 它测到的链路

    身份 → 拒绝写反馈表 → **新会话**读回 → 并进 `rejected_variant_ids`
    ⇒ 推荐链排除它 ⇒ 断言「它不再出现」✓（三段缺一段这题就红 ✓）。

    ⚠️ 需要 `DURABLE_PREFS_MODE=on`：`off` 模式不写也不读反馈 ⇒ 断言必然红 ✓
    （这正是**反证**的形状 ✓）。
    """
    out = []
    for i, req in enumerate(requirements):
        reason = rng.choice(REJECT_PHRASES)
        car = req["series_name"]
        out.append(
            {
                "id": f"mt_remembered_{i + 1:04d}",
                "scenario": "rejection_remembered_across_sessions",
                "enabled": enabled,
                "reason_disabled": "依赖 Phase 2b：拒绝反馈跨会话记住（反馈表 + 身份）",
                "turns": [
                    {
                        "user": _recommend_turn(req),
                        "assert": [],
                        "session": "first",
                    },
                    {
                        "user": f"{car}不要，{reason}",
                        "assert": [],
                        "session": "first",
                    },
                    {
                        # **新会话**（同一身份）：那台车不该再回来
                        "user": _recommend_turn(req),
                        "assert": [{"type": "not_recommended", "car": car}],
                        "session": "second",
                    },
                ],
            }
        )
    return out


def gen_exclusion_memory_scenarios(
    rng: random.Random, requirements: list[dict], db, *, enabled: bool = False
) -> list[dict]:
    """场景 5：排除项**跨会话**还记得吗？（说「不要 X 品牌」→ 换会话 → 该品牌不再出现）

    ## 为什么必须单独有一类（2026-10-21）

    计划 §七的第一条判断是「**硬约束排除项**是记忆的第一价值」，
    而在此之前跨会话题库里**根本没有排除项这一类**：预算（回访/删除）与拒绝（跨会话）都验过，
    **唯独「排除项」没验过** ⇒ 那条判断在**评测层面是空的**（实测：生产三张新表长期 0 行，
    排除项从没被真正跑起来过）。

    ## 断言为什么不会恒真

    「该品牌不该再出现」在**没有记忆**时也可能为真（那个品牌本来就不在榜）⇒
    所以生成时**先验一遍**：**不带**排除项时跑一次真实推荐，
    那个品牌的车**确实在榜**才收 ⇒ 断言有对象可验（否则 off 档也会「通过」⇒ 白验）。
    """
    out: list[dict] = []
    for i, req in enumerate(requirements):
        brand = str(req.get("brand") or "")
        if not brand:
            continue
        lo_w, hi_w = req["band"]
        # 不带排除项的需求句（用于验「那个品牌本来就在榜」）
        base_turn = (
            f"预算{lo_w}到{hi_w}万，{req['body_cn']}，{req['energy_cn']}的，"
            f"{req['usage_cn']}为主，家里{req['passengers']}口人用"
        )
        try:
            pairs = _recommended_series_brands(db, base_turn)
        except Exception:  # noqa: BLE001 —— 单个候选验不过不该让整批失败
            continue
        if not any(b == brand for _name, b in pairs):
            # 该品牌本来就不在榜 ⇒ 「它不该再出现」是**恒真**的 ⇒ 不收
            continue
        out.append(
            {
                "id": f"mt_exclusion_{i + 1:04d}",
                "scenario": "exclusion_memory",
                "enabled": enabled,
                "reason_disabled": "依赖 Phase 1：持久化身份 + 偏好记忆（排除项回填）",
                "turns": [
                    {
                        "user": f"{base_turn}，不要{brand}",
                        "assert": [],
                        "session": "first",
                    },
                    {
                        # ⚠️ 第二轮**重复硬约束**（预算/车身/能源/用途/人数）但**不重复排除项**。
                        #
                        # 为什么不能只问「按我之前说的条件推荐」：**没有记忆时**（off 档）
                        # 引擎手里没有预算 ⇒ 它会**反问**「预算大概多少？」
                        # ⇒ 推荐列表是**空的** ⇒ 「该品牌不在榜」**白送**（断言恒真）。
                        # 实测：那样生成的话，off 档 23/23 全过 ⇒ 这类场景证明不了任何事。
                        #
                        # 重复硬约束后，off 档**也能推出一批车** ⇒ 「那个品牌在不在榜」
                        # 才是真判据 ⇒ 记忆生效时它不在（断言成立），
                        # 记忆不生效时它**应该**在（断言红）⇒ 这一类才测得住。
                        "user": f"还是{base_turn}",
                        "assert": [{"type": "brand_not_recommended", "brand": brand}],
                        "session": "second",
                    },
                ],
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成多轮场景题库")
    parser.add_argument("--out", default="eval/multiturn.json", help="输出路径")
    parser.add_argument("--count", type=int, default=50, help="每个场景多少题")
    parser.add_argument("--seed", type=int, default=20261020, help="随机种子（固定以保证可复现）")
    parser.add_argument(
        "--enable-memory-scenarios",
        action="store_true",
        help=(
            "启用四个**跨会话**场景（回访记忆 / 删记忆 / 拒绝跨会话记住 / "
            "**排除项跨会话**；默认关）。需要："
            "引擎 handle 支持 identity + DURABLE_PREFS_MODE=on"
            "（off/shadow 模式不回填记忆、不读写反馈，断言必然红）"
        ),
    )
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    # ⚠️ 会话必须**开着**直到场景生成完：排除项场景要跑真实推荐来验
    # 「那个品牌本来就在榜」（否则断言恒真）⇒ 不能在 with 块外用它。
    with get_session_factory()() as db:
        requirements = sample_requirements(db, rng, args.count)
        if len(requirements) < args.count:
            print(
                f"⚠️ 库里只抽到 {len(requirements)} 个可满足的需求（要 {args.count} 个）——"
                f"请检查库里在售车系的价格档与车身/能源标注。"
            )
        scenarios = (
            gen_reject_scenarios(rng, requirements)
            + gen_returning_scenarios(
                rng, requirements, enabled=args.enable_memory_scenarios
            )
            + gen_memory_deleted_scenarios(
                rng, requirements, enabled=args.enable_memory_scenarios
            )
            + gen_rejection_remembered_scenarios(
                rng, requirements, enabled=args.enable_memory_scenarios
            )
            + gen_exclusion_memory_scenarios(
                rng, requirements, db, enabled=args.enable_memory_scenarios
            )
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "seed": args.seed,
                "scenarios": scenarios,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    enabled = sum(1 for s in scenarios if s.get("enabled"))
    print(f"已生成 {len(scenarios)} 个场景 -> {out}")
    print(f"  启用 {enabled}，待启用 {len(scenarios) - enabled}（依赖 Phase 1）")

    # 记录变量分布，便于人工核查覆盖度。
    # ⚠️ 品牌数从**生成时的输入**统计，不要从题面文本里 split——
    # 题面是「预算18到25万，埃安的轿车，燃油的，...」，按「的」拆会得到垃圾。
    brands = set()
    cars = set()
    for s in scenarios:
        for t in s["turns"]:
            u = t["user"]
            if "不要" in u:
                cars.add(u.split("不要")[0])
        brands.add(s["turns"][0]["user"].split("，")[1].split("的")[0])
    print(f"  品牌覆盖 {len(brands)} 个，车系覆盖 {len(cars)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
