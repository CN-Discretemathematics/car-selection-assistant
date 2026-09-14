"""汽车之家全量 SKU（款型/参数配置）抓取→导入 CLI。

覆盖范围：主要 ToC 新能源品牌及子品牌 + BBA/二线豪华/
常见日系/美系/大众及其他主流家用品牌（品牌注册表见 IN_SCOPE_BRANDS）。

流程三阶段（可断点续传）：
  index  → 抓取 A-Z 品牌车系索引，按品牌注册表过滤在范围车系；
  series → 逐车系抓取详情页（品牌/级别/能源/指导价/图片）并导入；
  sku    → 逐车系抓取参数配置接口（getParamConf）→ 款型/价格/参数事实导入。

用法：
    python tools/fetch_autohome_sku.py --stage index
    python tools/fetch_autohome_sku.py --stage series --limit 5
    python tools/fetch_autohome_sku.py --stage sku --ids 7806,692
    python tools/fetch_autohome_sku.py --stage all
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sources.autohome import (  # noqa: E402
    build_series_payload,
    fetch_robots,
    parse_series_page,
)
from app.sources.autohome_sku import (  # noqa: E402
    SKU_API,
    build_sku_payload,
    decode_autohome_html,
    fetch_series_index,
    fetch_sku_config,
    parse_series_index,
)
from app.sources.fetcher import DEFAULT_USER_AGENT  # noqa: E402

SNAPSHOT_DIR = os.path.join("snapshots")
RAW_SKU_DIR = os.path.join("snapshots", "raw", "sku")
INDEX_PATH = os.path.join(SNAPSHOT_DIR, "autohome-series-index.json")
SCOPE_PATH = os.path.join(SNAPSHOT_DIR, "autohome-in-scope-series.json")
DETAIL_PATH = os.path.join(SNAPSHOT_DIR, "autohome-series-detail-all.json")
CHECKPOINT_PATH = os.path.join(SNAPSHOT_DIR, "autohome-sku-checkpoint.json")

SLEEP_SECONDS = 1.2  # 礼貌限频（≥1s/请求）
MAX_RETRIES = 3
# 汽车之家来源名（external_series_refs 的唯一键是 (source_id, external_id)，不同来源的
# external_id 可能重号，故按来源限定，避免将来接入第二个来源时抓到别的车系）
AUTOHOME_SOURCE_NAME = "汽车之家"


def _explicit_scope(ids: list[str], session=None) -> list[dict]:
    """把 `--ids` 指定的 seriesid 变成 scope 同构条目（元数据从数据库补）。

    为什么需要（2026-09-14 实测事故）：A-Z 索引会漏车系——「凯美瑞」（seriesid=110）
    不在索引进而也不在 scope 里，但接口 `fetch_sku_config('110')` 正常返回 13 款型。
    此前 `--ids` 只在 scope **内部**过滤，于是这些车系每次调用都是「待抓取 0 个」，
    上层脚本看到退出码 0 就记成完成 → 171 个车系静默无款型。
    `--ids` 的 id 来自库内 `external_series_refs`（与销量榜同源，已核对与配置页同一 id 空间），
    本身可信，因此显式指定时不再受索引范围限制；元数据（品牌/类别/车系名/价格区间）回落数据库。
    """
    from sqlalchemy import select

    from app.common.models import Brand, ExternalSeriesRef, Source, VehicleSeries
    from app.common.database import get_session_factory

    own_session = session is None
    session = session or get_session_factory()()
    entries: list[dict] = []
    try:
        for external_id in ids:
            row = session.execute(
                select(VehicleSeries, Brand)
                .join(ExternalSeriesRef, ExternalSeriesRef.series_id == VehicleSeries.id)
                .join(Source, Source.id == ExternalSeriesRef.source_id)
                .join(Brand, VehicleSeries.brand_id == Brand.id)
                .where(
                    ExternalSeriesRef.external_id == external_id,
                    Source.name == AUTOHOME_SOURCE_NAME,
                )
                .limit(1)
            ).first()
            if row is None:
                print(f"  跳过 {external_id}：库里没有对应映射", file=sys.stderr)
                continue
            series, brand = row
            entries.append(
                {
                    "external_id": external_id,
                    "name": series.name,
                    # 门户价格区间原文（库内已有则带上，缺失时留空由导入侧补）
                    "price_note": series.price_range_note or "",
                    "brand": brand.name,
                    "brand_type": brand.brand_type,
                    # 不编造收录理由：库里没有就留空，由导入侧按既有默认处理（第二轮审查 m2）
                    "inclusion_reason": brand.inclusion_reason,
                }
            )
    finally:
        if own_session:
            session.close()
    return entries

# 品牌注册表：纳入范围与品牌类别（类别可在管理后台再调整）
IN_SCOPE_BRANDS: dict[str, dict] = {
    # 主要 ToC 新能源品牌及子品牌
    "比亚迪": {"brand_type": "domestic_nev", "inclusion_reason": "主流新能源品牌（§2.1）"},
    "腾势": {"brand_type": "domestic_nev", "inclusion_reason": "比亚迪子品牌"},
    "方程豹": {"brand_type": "domestic_nev", "inclusion_reason": "比亚迪子品牌"},
    "仰望": {"brand_type": "domestic_nev", "inclusion_reason": "比亚迪子品牌"},
    "理想": {"brand_type": "domestic_nev", "inclusion_reason": "主流新能源品牌（§2.1）"},
    "蔚来": {"brand_type": "domestic_nev", "inclusion_reason": "主流新能源品牌（§2.1）"},
    "乐道": {"brand_type": "domestic_nev", "inclusion_reason": "蔚来子品牌"},
    "小鹏": {"brand_type": "domestic_nev", "inclusion_reason": "主流新能源品牌（§2.1）"},
    "零跑": {"brand_type": "domestic_nev", "inclusion_reason": "主流新能源品牌（§2.1）"},
    # 注：哪吒、飞凡当前已不在汽车之家品牌索引（下架/并入其他品牌），暂无门户数据可抓
    "极氪": {"brand_type": "domestic_nev", "inclusion_reason": "主流新能源品牌（§2.1）"},
    "深蓝": {"brand_type": "domestic_nev", "inclusion_reason": "长安新能源子品牌"},
    "阿维塔": {"brand_type": "domestic_nev", "inclusion_reason": "长安新能源子品牌"},
    "启源": {"brand_type": "domestic_nev", "inclusion_reason": "长安新能源子品牌"},
    "岚图": {"brand_type": "domestic_nev", "inclusion_reason": "东风新能源子品牌"},
    "奕派": {"brand_type": "domestic_nev", "inclusion_reason": "东风新能源子品牌"},
    "智己": {"brand_type": "domestic_nev", "inclusion_reason": "上汽新能源子品牌"},
    "昊铂": {"brand_type": "domestic_nev", "inclusion_reason": "广汽新能源子品牌"},
    "埃安": {"brand_type": "domestic_nev", "inclusion_reason": "主流新能源品牌（§2.1）"},
    "欧拉": {"brand_type": "domestic_nev", "inclusion_reason": "长城新能源子品牌"},
    "极狐": {"brand_type": "domestic_nev", "inclusion_reason": "北汽新能源子品牌"},
    "创维": {"brand_type": "domestic_nev", "inclusion_reason": "新能源品牌"},
    "银河": {"brand_type": "domestic_nev", "inclusion_reason": "吉利新能源子品牌"},
    "几何": {"brand_type": "domestic_nev", "inclusion_reason": "吉利新能源子品牌"},
    "睿蓝": {"brand_type": "domestic_nev", "inclusion_reason": "吉利新能源子品牌"},
    "蓝电": {"brand_type": "domestic_nev", "inclusion_reason": "赛力斯子品牌"},
    "iCAR": {"brand_type": "domestic_nev", "inclusion_reason": "奇瑞新能源子品牌"},
    "小米汽车": {"brand_type": "domestic_nev", "inclusion_reason": "主流新能源品牌（§2.1）"},
    "问界": {"brand_type": "domestic_nev", "inclusion_reason": "鸿蒙智行主力品牌"},
    "智界": {"brand_type": "domestic_nev", "inclusion_reason": "鸿蒙智行子品牌"},
    "享界": {"brand_type": "domestic_nev", "inclusion_reason": "鸿蒙智行子品牌"},
    "尊界": {"brand_type": "domestic_nev", "inclusion_reason": "鸿蒙智行子品牌"},
    "尚界": {"brand_type": "domestic_nev", "inclusion_reason": "鸿蒙智行子品牌"},
    "极星": {"brand_type": "domestic_nev", "inclusion_reason": "吉利系新能源品牌"},
    "特斯拉": {"brand_type": "american", "inclusion_reason": "主流新能源品牌（§2.1）"},
    # BBA
    "奥迪": {"brand_type": "luxury", "inclusion_reason": "BBA（§2.1）"},
    "奔驰": {"brand_type": "luxury", "inclusion_reason": "BBA（§2.1）"},
    "宝马": {"brand_type": "luxury", "inclusion_reason": "BBA（§2.1）"},
    "MINI": {"brand_type": "luxury", "inclusion_reason": "宝马集团品牌"},
    # 二线豪华
    "凯迪拉克": {"brand_type": "luxury", "inclusion_reason": "二线豪华（§2.1）"},
    "雷克萨斯": {"brand_type": "luxury", "inclusion_reason": "二线豪华（§2.1）"},
    "沃尔沃": {"brand_type": "luxury", "inclusion_reason": "二线豪华（§2.1）"},
    "林肯": {"brand_type": "luxury", "inclusion_reason": "二线豪华（§2.1）"},
    "捷豹": {"brand_type": "luxury", "inclusion_reason": "二线豪华（§2.1）"},
    "路虎": {"brand_type": "luxury", "inclusion_reason": "二线豪华（§2.1）"},
    "英菲尼迪": {"brand_type": "luxury", "inclusion_reason": "二线豪华（§2.1）"},
    "捷尼赛思": {"brand_type": "luxury", "inclusion_reason": "二线豪华（§2.1）"},
    # 常见日系
    "丰田": {"brand_type": "japanese", "inclusion_reason": "常见日系（§2.1）"},
    "本田": {"brand_type": "japanese", "inclusion_reason": "常见日系（§2.1）"},
    "日产": {"brand_type": "japanese", "inclusion_reason": "常见日系（§2.1）"},
    "马自达": {"brand_type": "japanese", "inclusion_reason": "常见日系（§2.1）"},
    "斯巴鲁": {"brand_type": "japanese", "inclusion_reason": "常见日系（§2.1）"},
    # 美系
    "别克": {"brand_type": "american", "inclusion_reason": "美系（§2.1）"},
    "雪佛兰": {"brand_type": "american", "inclusion_reason": "美系（§2.1）"},
    "福特": {"brand_type": "american", "inclusion_reason": "美系（§2.1）"},
    "Jeep": {"brand_type": "american", "inclusion_reason": "美系（§2.1）"},
    # 大众系
    "大众": {"brand_type": "german", "inclusion_reason": "大众（§2.1）"},
    "捷达": {"brand_type": "german", "inclusion_reason": "大众集团品牌"},
    # 其他主流家用
    "吉利汽车": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "领克": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "哈弗": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "坦克": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "魏牌": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "长城": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "奇瑞": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "星途": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "捷途": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "长安": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "荣威": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "名爵": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "传祺": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "奔腾": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "红旗": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "五菱": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "宝骏": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "风神": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "风行": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "东风": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "江淮汽车": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "北京": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "大通": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "现代": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "起亚": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "标致": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "雪铁龙": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
    "启辰": {"brand_type": "other_fuel", "inclusion_reason": "主流家用（§2.1）"},
}

# 汽车之家索引品牌名 → 注册表名（子品牌/命名差异）
BRAND_ALIASES = {
    "吉利": "吉利汽车",
    "东风奕派": "奕派",
    "东风风神": "风神",
    "东风风行": "风行",
    "长安启源": "启源",
    "小米": "小米汽车",
    "AITO 问界": "问界",
    "ARCFOX极狐": "极狐",
    "创维汽车": "创维",
    "吉利银河": "银河",
    "吉利几何": "几何",
    "北京汽车": "北京",
    "北京越野": "北京",
    "广汽传祺": "传祺",
    "广汽昊铂": "昊铂",
    "岚图汽车": "岚图",
    "智己汽车": "智己",
    "理想汽车": "理想",
    "睿蓝汽车": "睿蓝",
    "零跑汽车": "零跑",
    "深蓝汽车": "深蓝",
    "五菱汽车": "五菱",
    "MG": "名爵",
    "Polestar极星": "极星",
    "奥迪AUDI": "奥迪",
    "奇瑞新能源": "奇瑞",
    "奇瑞QQ": "奇瑞",
    "奇瑞风云": "奇瑞",
    "捷途山海": "捷途",
    "长安欧尚": "长安",
    "江淮钇为": "江淮汽车",
    "江淮瑞风": "江淮汽车",
}


def _load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load_checkpoint() -> dict:
    return _load_json(
        CHECKPOINT_PATH,
        {"series_pages_done": {}, "sku_done": {}, "failures": {}, "compliance": {}},
    )


def _save_checkpoint(cp: dict) -> None:
    _save_json(CHECKPOINT_PATH, cp)


def _record_compliance(cp: dict, host: str, policy) -> None:
    """把 robots 检查结论写入断点文件（§20 合规留痕：检查过的 host/时间/结论）。"""
    fetched = getattr(policy, "fetched_at", None) is not None
    cp.setdefault("compliance", {})[host] = {
        "robots_fetched": fetched,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "robots.txt 已检查并遵守" if fetched else "robots.txt 不可达，按默认允许并已人工确认条款",
    }
    _save_checkpoint(cp)


def _canonical_brand(name: str) -> str | None:
    if name in IN_SCOPE_BRANDS:
        return name
    return BRAND_ALIASES.get(name)


def _brand_meta_for(name: str) -> tuple[str, dict] | None:
    """把任意品牌名（索引名/车系页名）归一到注册表：返回 (标准名, 注册表元数据)。"""
    canonical = _canonical_brand(name)
    if canonical:
        return canonical, IN_SCOPE_BRANDS[canonical]
    return None


def _build_scope(index_data: dict) -> list[dict]:
    """按品牌注册表过滤在范围车系。返回 [{brand, brand_type, inclusion_reason, external_id, name, price_note}]。"""
    scope: list[dict] = []
    seen_brands: set[str] = set()
    for block in index_data["brands"]:
        canonical = _canonical_brand(block["name"])
        if canonical is None:
            continue
        seen_brands.add(canonical)
        meta = IN_SCOPE_BRANDS[canonical]
        for s in block["series"]:
            scope.append(
                {
                    "brand": canonical,
                    "brand_type": meta["brand_type"],
                    "inclusion_reason": meta["inclusion_reason"],
                    "external_id": s["external_id"],
                    "name": s["name"],
                    "price_note": s["price_note"],
                }
            )
    return scope, seen_brands


# ── 阶段 1：品牌车系索引 ─────────────────────────────────────────

def stage_index(_args) -> int:
    print("robots 检查（www.autohome.com.cn）…")
    policy = fetch_robots("https://www.autohome.com.cn")
    if not policy.allowed("/grade/", DEFAULT_USER_AGENT):
        print("robots.txt 禁止 /grade/，终止", file=sys.stderr)
        return 1
    cp = _load_checkpoint()
    _record_compliance(cp, "www.autohome.com.cn", policy)
    blocks: list[dict] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        html, url = None, None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                html, url = fetch_series_index(letter)
                break
            except Exception as err:  # noqa: BLE001
                if attempt == MAX_RETRIES:
                    print(f"{letter} 失败：{type(err).__name__}: {err}", file=sys.stderr)
                    return 1
                time.sleep(3.0)
        parsed = parse_series_index(html)
        blocks.extend(parsed["brands"])
        print(f"{letter}: {len(parsed['brands'])} 个品牌块 ({url})")
        time.sleep(SLEEP_SECONDS)
    index_data = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "brands": blocks}
    _save_json(INDEX_PATH, index_data)
    print(f"索引已保存：{INDEX_PATH}（{len(blocks)} 个品牌块）")

    scope, seen = _build_scope(index_data)
    _save_json(SCOPE_PATH, scope)
    matched = len(IN_SCOPE_BRANDS) - len(set(IN_SCOPE_BRANDS) - seen)
    print(f"在范围：{len(scope)} 个车系；注册表品牌命中 {matched}/{len(IN_SCOPE_BRANDS)}")
    missing = sorted(set(IN_SCOPE_BRANDS) - seen)
    if missing:
        print("未命中的注册表品牌（确认索引名或加别名）：", ", ".join(missing))
    return 0


# ── 阶段 2：车系详情 ─────────────────────────────────────────────

def stage_series(args) -> int:
    scope = _load_json(SCOPE_PATH)
    cp = _load_checkpoint()
    detail_rows = _load_json(DETAIL_PATH, [])
    detail_map = {str(r["external_id"]): r for r in detail_rows}
    if args.ids:
        # 与 stage_sku 同口径：显式 id 不受索引范围限制，元数据从库内补
        want = [i.strip() for i in args.ids.split(",") if i.strip()]
        known = {str(s["external_id"]): s for s in (scope or [])}
        missing = [i for i in want if i not in known]
        filled = {s["external_id"]: s for s in _explicit_scope(missing)} if missing else {}
        resolved = [known.get(i) or filled.get(i) for i in want]
        pending = [s for s in resolved if s and str(s["external_id"]) not in cp["series_pages_done"]]
    else:
        if not scope:
            print("先运行 --stage index", file=sys.stderr)
            return 1
        pending = [s for s in scope if str(s["external_id"]) not in cp["series_pages_done"]]
    if args.limit:
        pending = pending[: args.limit]
    # 显式指定了 id 却一个都没解析出来（库里无映射）→ 非 0，避免「待抓取 0 个 + 退出码 0」
    # 被上层误读为完成（第二轮审查 m7）
    if args.ids and not pending and want:
        print(f"错误：--ids 指定的 {len(want)} 个车系均无有效映射或都已完成", file=sys.stderr)
        return 1
    print(f"车系详情：待抓取 {len(pending)} 个（限频 {SLEEP_SECONDS}s/页）")

    # robots 合规：PC 车系详情页路径逐一核对（policy 本地判定，不额外发请求）
    policy = fetch_robots("https://www.autohome.com.cn")
    _record_compliance(cp, "www.autohome.com.cn", policy)

    from app.sources.importer import import_catalog

    for i, s in enumerate(pending, 1):
        sid = s["external_id"]
        try:
            if not policy.allowed(f"/{sid}/", DEFAULT_USER_AGENT):
                raise PermissionError(f"robots.txt 禁止 /{sid}/")
            row = _fetch_series_page_with_retry(sid)
            row["price_note"] = _price_note_from_row(row) or s["price_note"]
            row["index_name"] = s["name"]
            detail_map[sid] = row
            if not args.no_import:
                payload = build_series_payload([row], f"https://www.autohome.com.cn/{sid}/")
                # 品牌一律归一到范围清单中的标准品牌名（车系页 brandName 可能是子品牌
                # 命名差异，如「北京越野」「奥迪AUDI」「上汽大通MAXUS」）。
                # 注意：`--ids` 放开了索引范围后，库内品牌不一定在注册表里（如销量榜导入
                # 产生的「待分类（汽车之家销量榜）」）——此时按车系页品牌原样导入，不做改写，
                # 否则会 KeyError 导致整批失败（2026-09-14 第二轮审查 M1）。
                canonical = s["brand"]
                reg = IN_SCOPE_BRANDS.get(canonical)
                if reg:
                    for b in payload["brands"]:
                        b["name"] = canonical
                        b["brand_type"] = reg["brand_type"]
                        b["inclusion_reason"] = reg["inclusion_reason"]
                    for s_cfg in payload["series"]:
                        s_cfg["brand"] = canonical
                else:
                    print(f"  提示：品牌「{canonical}」不在收录注册表内，按车系页品牌原样导入")
                _import_catalog_once(payload)
            cp["series_pages_done"][sid] = "ok"
            cp["failures"].pop(sid, None)
            print(f"[{i}/{len(pending)}] {sid} {s['name']} ok")
        except Exception as err:  # noqa: BLE001
            cp["failures"][sid] = f"{type(err).__name__}: {err}"
            print(f"[{i}/{len(pending)}] {sid} {s['name']} 失败：{err}")
        _save_json(DETAIL_PATH, sorted(detail_map.values(), key=lambda r: r["external_id"]))
        _save_checkpoint(cp)
        time.sleep(SLEEP_SECONDS)
    print(f"阶段 series 完成：{len(cp['series_pages_done'])} 成功 / {len(cp['failures'])} 失败")
    # 退出码必须反映结果：全失败却返回 0 会让上层把失败记成完成（第二轮审查 M1）
    return 1 if cp["failures"] else 0


def _fetch_series_page_with_retry(sid: str) -> dict:
    import urllib.request

    url = f"https://www.autohome.com.cn/{sid}/"
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = decode_autohome_html(resp.read(), resp.headers.get("Content-Type") or "")
            return parse_series_page(html, sid)
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt < MAX_RETRIES:
                time.sleep(3.0)
    raise last_err  # type: ignore[misc]


def _price_note_from_row(row: dict) -> str | None:
    from app.sources.autohome import format_price_note

    if row.get("min_price") is None and row.get("max_price") is None:
        return None
    return format_price_note(row.get("min_price"), row.get("max_price"))


def _import_catalog_once(payload: dict) -> None:
    from app.common.database import create_all, get_session_factory

    create_all()
    factory = get_session_factory()
    with factory() as db:
        from app.sources.importer import import_catalog

        report = import_catalog(db, payload)
        if report.errors:
            raise RuntimeError("; ".join(report.errors[:5]))


# ── 阶段 3：SKU 参数配置 ─────────────────────────────────────────

def stage_sku(args) -> int:
    scope = _load_json(SCOPE_PATH)
    cp = _load_checkpoint()
    detail_map = {str(r["external_id"]): r for r in (_load_json(DETAIL_PATH) or [])}
    # robots 合规留痕：移动端参数配置接口 host（接口域 robots 不可达时按默认允许，
    # 结论与时间写入断点文件）
    for host in ("car-web-m.autohome.com.cn", "car.m.autohome.com.cn"):
        _record_compliance(cp, host, fetch_robots(f"https://{host}"))
    if args.ids:
        # 显式指定 id：不受 A-Z 索引范围限制（索引会漏车系，见 _explicit_scope 注释），
        # 元数据从数据库补齐；已完成的仍然跳过
        want = [i.strip() for i in args.ids.split(",") if i.strip()]
        known = {str(s["external_id"]): s for s in (scope or [])}
        missing = [i for i in want if i not in known]
        filled = {s["external_id"]: s for s in _explicit_scope(missing)} if missing else {}
        resolved = [known.get(i) or filled.get(i) for i in want]
        pending = [s for s in resolved if s and str(s["external_id"]) not in cp["sku_done"]]
    else:
        if not scope:
            print("先运行 --stage index", file=sys.stderr)
            return 1
        pending = [s for s in scope if str(s["external_id"]) not in cp["sku_done"]]
    if args.limit:
        pending = pending[: args.limit]
    # 同 stage_series：显式 id 全部无法解析时返回非 0（第二轮审查 m7）
    if args.ids and not pending and want:
        print(f"错误：--ids 指定的 {len(want)} 个车系均无有效映射或都已完成", file=sys.stderr)
        return 1
    print(f"SKU 参数配置：待抓取 {len(pending)} 个车系（限频 {SLEEP_SECONDS}s/请求）")

    for i, s in enumerate(pending, 1):
        sid = s["external_id"]
        try:
            parsed = _fetch_sku_with_retry(sid)
            detail = detail_map.get(sid) or {}
            series_meta = {
                "external_id": sid,
                "name": detail.get("name") or s["name"],
                "energy_types": (_detail_energy_types(detail)
                                 if detail.get("fueltypes") else []),
                "price_note": detail.get("price_note") or s["price_note"],
                "thumbnail_url": detail.get("logo") or None,
            }
            payload = build_sku_payload(
                {"name": s["brand"], "brand_type": s["brand_type"],
                 "inclusion_reason": s["inclusion_reason"]},
                series_meta,
                parsed,
                f"https://car.m.autohome.com.cn/config/series/{sid}.html",
            )
            if args.save_raw:
                _save_raw_sku(sid, payload)
            if not args.no_import:
                _import_catalog_once(payload)
            n_variants = sum(len(y["variants"]) for y in payload["series"][0].get("model_years", []))
            if n_variants <= 0:
                # 抓到 0 款型**不得记为完成**：否则该车系被永久跳过（2026-09-14 事故：
                # 171 个车系就是这样被标记完成的，缺口一直不收敛）
                cp["failures"][sid] = "SKU 接口返回 0 款型"
                print(f"[{i}/{len(pending)}] {sid} {s['name']} 抓到 0 款型，记为失败待重试")
            else:
                cp["sku_done"][sid] = "ok"
                cp["failures"].pop(sid, None)
                print(f"[{i}/{len(pending)}] {sid} {s['name']} ok（{n_variants} 款型）")
        except Exception as err:  # noqa: BLE001
            cp["failures"][sid] = f"{type(err).__name__}: {err}"
            print(f"[{i}/{len(pending)}] {sid} {s['name']} 失败：{err}")
        _save_checkpoint(cp)
        time.sleep(SLEEP_SECONDS)
    print(f"阶段 sku 完成：{len(cp['sku_done'])} 成功 / {len(cp['failures'])} 失败")
    return 0


def _detail_energy_types(detail: dict) -> list[str]:
    from app.sources.autohome import map_energy_types

    return map_energy_types(detail.get("fueltypes") or "", detail.get("energytype"))


def _fetch_sku_with_retry(sid: str) -> dict:
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_sku_config(sid)
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt < MAX_RETRIES:
                time.sleep(3.0)
    raise last_err  # type: ignore[misc]


def _save_raw_sku(sid: str, payload: dict) -> None:
    os.makedirs(RAW_SKU_DIR, exist_ok=True)
    path = os.path.join(RAW_SKU_DIR, f"{sid}.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK：强制 UTF-8，避免长中文/特殊字符打印崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="汽车之家全量 SKU 抓取→导入")
    parser.add_argument("--stage", default="all", choices=["index", "series", "sku", "all"])
    parser.add_argument("--limit", type=int, default=0, help="本次最多处理的系列数（0=全部）")
    parser.add_argument("--ids", default=None, help="逗号分隔的 seriesid，仅处理这些系列")
    parser.add_argument("--no-import", action="store_true", help="只抓取与生成载荷，不导入")
    parser.add_argument("--save-raw", action="store_true", help="每个车系的载荷另存 gzip 快照")
    args = parser.parse_args(argv)

    stages = ["index", "series", "sku"] if args.stage == "all" else [args.stage]
    for stage in stages:
        rc = {"index": stage_index, "series": stage_series, "sku": stage_sku}[stage](args)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
