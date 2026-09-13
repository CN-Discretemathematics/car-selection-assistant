"""汽车之家销量榜数据接入（备用销量来源）。

合规（条款已由项目所有者确认允许）：
- 抓取前检查 robots.txt（/rank 未被禁止）；
- 自定义 UA、单页请求、频率 ≥1 秒；
- 榜单口径为「门户榜单口径」（portal），不冒充统一零售口径，展示时如实标注；
- 榜单不含品牌字段（仅 brandid），车系先挂「待分类（汽车之家销量榜）」品牌，
  待车型数据导入后由管理后台归并。
"""
from __future__ import annotations

import json
import logging
import re
import time

from app.sources.fetcher import DEFAULT_USER_AGENT, fetch_robots

logger = logging.getLogger(__name__)

RANK_URL_TEMPLATE = "https://www.autohome.com.cn/rank/1-1-0-0_9000-x-x-x/{month}.html"
# 榜单数据接口（页面首屏只 SSR 20 行，完整榜单由该接口按 pageindex/pagesize 提供；
# 2026-09 实测 pagesize=1000 一次返回 650 行 = 当月全部有销量的车系）。
# 参数与页面自身请求一致（typeid=1 车系月销榜、subranktypeid=1、levelid=0 全部、price=0-9000 不限价）。
RANK_API_URL = "https://www.autohome.com.cn/web-main/car/rank/getList"
RANK_API_BASE_PARAMS = {
    "from": "28",
    "pm": "2",
    "pluginversion": "11.75.8",
    "model": "1",
    "channel": "0",
    "typeid": "1",
    "subranktypeid": "1",
    "levelid": "0",
    "price": "0-9000",
}
# 每页行数：接口实测支持 1000；取 200 兼顾单次响应体大小与请求数（650 行 ≈ 4 次请求）
RANK_API_PAGE_SIZE = 200
# 翻页上限（防御：接口异常返回超大 pagecount 时不至于无限抓取）
RANK_API_MAX_PAGES = 12
PLACEHOLDER_BRAND = "待分类（汽车之家销量榜）"
BASE_URL = "https://www.autohome.com.cn"


def parse_rank_page(html: str) -> dict:
    """从榜单页面提取 __NEXT_DATA__ 中的车系销量行。"""
    marker = "__NEXT_DATA__"
    i = html.find(marker)
    if i < 0:
        raise ValueError("页面未包含榜单数据（__NEXT_DATA__）")
    start = html.find(">", i) + 1
    end = html.find("</script>", start)
    if end < 0:
        raise ValueError("__NEXT_DATA__ 未闭合")
    data = json.loads(html[start:end])
    props = data.get("props", {}).get("pageProps", {})
    month = (props.get("initialValues") or {}).get("date")
    rows = []
    for item in (props.get("listRes") or {}).get("list") or []:
        count = item.get("salecount")
        if count is None or not str(count).isdigit():
            continue
        rows.append(
            {
                "rank": item.get("rankNum"),
                "external_id": str(item.get("seriesid") or ""),
                "seriesname": item.get("seriesname") or "",
                "salecount": int(count),
            }
        )
    return {"month": month, "rows": rows}


def build_payload(parsed: dict, page_url: str) -> dict:
    """把榜单行转换为 importer 兼容载荷（销量口径 portal）。"""
    month = parsed["month"]
    series = [
        {"brand": PLACEHOLDER_BRAND, "name": row["seriesname"], "external_id": row["external_id"]}
        for row in parsed["rows"]
    ]
    sales = [
        {
            "external_id": row["external_id"],
            "series": row["seriesname"],
            "month": month,
            "sales_type": "portal",
            "count": row["salecount"],
        }
        for row in parsed["rows"]
    ]
    return {
        "source": {
            "name": "汽车之家",
            "source_type": "industry_data",
            "url": page_url,
            "verified_status": "unverified",
            "credibility": "medium",
        },
        "brands": [
            {
                "name": PLACEHOLDER_BRAND,
                "brand_type": "other_fuel",
                "inclusion_reason": "汽车之家销量榜未提供品牌字段（仅 brandid），待车型数据导入后归并",
            }
        ],
        "series": series,
        "sales": sales,
    }


def fetch_rank_page(month: str) -> tuple[str, str]:
    """robots 检查 → 抓取榜单页 → 返回 (html, url)。

    仅用于**判定门户当前公布的月份**（页面 SSR 的 initialValues.date）：门户未发布目标月时
    接口会静默返回上一月数据，需要一个可信的月份信号（fetch_and_import_month 依赖它做
    「未就绪则明日重试」）。完整榜单行走 fetch_rank_rows。
    """
    policy = fetch_robots(BASE_URL)
    if not policy.allowed("/rank/", DEFAULT_USER_AGENT):
        raise PermissionError("robots.txt 禁止访问 /rank/，请人工确认条款后调整")
    url = RANK_URL_TEMPLATE.format(month=month)
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    time.sleep(1.0)  # 礼貌抓取：频率下限 1 秒
    return html, url


# ── 榜单接口（完整榜单）─────────────────────────────────────────────────

def fetch_rank_api_page(
    month: str, pageindex: int = 1, pagesize: int = RANK_API_PAGE_SIZE
) -> dict:
    """抓取榜单接口的一页，返回原始 JSON（含 result.pagecount 等分页元数据）。"""
    import urllib.parse
    import urllib.request

    params = {
        **RANK_API_BASE_PARAMS,
        "date": month,
        "pageindex": str(pageindex),
        "pagesize": str(pagesize),
    }
    url = f"{RANK_API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": RANK_URL_TEMPLATE.format(month=month),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    time.sleep(0.4)  # 礼貌抓取：翻页间隔
    return payload


def parse_rank_api(payload: dict) -> dict:
    """把接口响应规范化为 {rows, pagecount, pageindex, pagesize}。

    rows 结构与 parse_rank_page 一致（rank/external_id/seriesname/salecount），
    以便下游 build_payload 完全复用；salecount 非数字的行（无销量/占位）跳过。
    """
    result = (payload or {}).get("result") or {}
    rows = []
    for item in result.get("list") or []:
        count = item.get("salecount")
        if count is None or not str(count).isdigit():
            continue
        rows.append(
            {
                "rank": item.get("rankNum") or item.get("rank"),
                "external_id": str(item.get("seriesid") or ""),
                "seriesname": item.get("seriesname") or "",
                "salecount": int(count),
            }
        )
    return {
        "rows": rows,
        "pagecount": int(result.get("pagecount") or 0),
        "pageindex": int(result.get("pageindex") or 0),
        "pagesize": int(result.get("pagesize") or 0),
    }


def fetch_rank_rows(
    month: str,
    *,
    pagesize: int = RANK_API_PAGE_SIZE,
    max_pages: int = RANK_API_MAX_PAGES,
) -> dict:
    """抓取目标月**完整**榜单（接口翻页），失败时回退到页面首屏。

    返回 {rows, pages, total_pages, source}；source ∈ {"api", "page-fallback"}。
    仅当接口整体不可用（网络/结构变化）才回退——回退只有 20 行，会显著降低覆盖率，
    因此调用方应把 source 记入报告，便于发现数据源变化。
    """
    try:
        first = fetch_rank_api_page(month, pageindex=1, pagesize=pagesize)
        parsed = parse_rank_api(first)
        rows = list(parsed["rows"])
        total_pages = max(min(parsed["pagecount"], max_pages), 1)
        for page in range(2, total_pages + 1):
            parsed_page = parse_rank_api(fetch_rank_api_page(month, pageindex=page, pagesize=pagesize))
            rows.extend(parsed_page["rows"])
        if rows:
            return {
                "rows": rows,
                "pages": total_pages,
                "total_pages": parsed["pagecount"],
                "source": "api",
            }
    except Exception:  # noqa: BLE001 - 接口异常回退页面首屏，不静默丢数据
        logger.exception("榜单接口抓取失败，回退页面首屏（Top 20）")

    html, _ = fetch_rank_page(month)
    fallback = parse_rank_page(html)
    return {
        "rows": fallback["rows"],
        "pages": 1,
        "total_pages": 1,
        "source": "page-fallback",
    }


# ── 车系详情页（品牌/级别/能源/指导价/图片，全部来自 seriesBaseInfo）────────

def parse_series_page(html: str, series_id: str) -> dict:
    """从车系页 __NEXT_DATA__ 提取 seriesBaseInfo 结构化字段。"""
    marker = "__NEXT_DATA__"
    i = html.find(marker)
    if i < 0:
        raise ValueError("页面未包含车系数据（__NEXT_DATA__）")
    start = html.find(">", i) + 1
    end = html.find("</script>", start)
    if end < 0:
        raise ValueError("__NEXT_DATA__ 未闭合")
    data = json.loads(html[start:end])
    info = (data.get("props", {}).get("pageProps", {}) or {}).get("seriesBaseInfo") or {}
    return {
        "external_id": str(series_id),
        "name": info.get("name") or "",
        "brand_name": info.get("brandName") or "",
        "level": info.get("levelName") or "",
        "min_price": info.get("minPrice"),
        "max_price": info.get("maxPrice"),
        "logo": info.get("logo") or "",
        "fueltypes": str(info.get("fueltypes") or ""),
        "energytype": info.get("energytype"),
    }


def map_energy_types(fueltypes: str, energytype) -> list[str]:
    """按门户字段推断系列能源类型（评审 M11 修正注释口径）：

    - energytype==1 为新能源系列（纯电/插混/增程按 fueltypes 细分：
      fueltypes 含 "5" → 插混/增程与纯电并存；否则纯电）；
    - energytype!=1 时按 fueltypes：含 "3" → 燃油/油混并存；否则燃油。
    系列内多动力取并集，展示层据此标注；SKU 级精确能源以款型为准。
    """
    if energytype == 1:
        if "5" in fueltypes:
            return ["BEV", "PHEV", "EREV"]
        return ["BEV"]
    if "3" in fueltypes:
        return ["ICE", "HEV"]
    return ["ICE"]


def map_body_type(level: str) -> str | None:
    if "SUV" in level:
        return "suv"
    if "MPV" in level:
        return "mpv"
    if "皮卡" in level:
        return "pickup"
    if level.endswith("车") or "轿车" in level:
        return "sedan"
    return None


def format_price_note(min_price, max_price) -> str | None:
    if min_price is None or max_price is None:
        return None
    if min_price == max_price:
        return f"{min_price / 10000:g}万元"
    return f"{min_price / 10000:g}-{max_price / 10000:g}万元"


def build_series_payload(rows: list[dict], page_url: str) -> dict:
    """把车系详情行转换为 importer 兼容载荷（品牌 + 车系更新，含指导价区间与图片）。"""
    brands: dict[str, dict] = {}
    series: list[dict] = []
    for row in rows:
        brand_name = row["brand_name"]
        if not brand_name:
            continue
        if brand_name not in brands:
            brands[brand_name] = {
                "name": brand_name,
                "brand_type": "other_fuel",  # 汽车之家未提供分类，品牌类别待人工确认
                "inclusion_reason": "来自汽车之家车系页（品牌分类待确认）",
            }
        series.append(
            {
                "brand": brand_name,
                "name": row["name"],
                "external_id": row["external_id"],
                "body_type": map_body_type(row["level"]),
                "positioning": row["level"] or None,
                "energy_types": map_energy_types(row["fueltypes"], row["energytype"]),
                "price_range_note": format_price_note(row["min_price"], row["max_price"]),
                "thumbnail_url": row["logo"] or None,
            }
        )
    return {
        "source": {
            "name": "汽车之家",
            "source_type": "industry_data",
            "url": page_url,
            "verified_status": "unverified",
            "credibility": "medium",
        },
        "brands": list(brands.values()),
        "series": series,
        "sales": [],
    }


def fetch_series_pages(series_ids: list[str], sleep_seconds: float = 1.0) -> tuple[list[dict], list[str]]:
    """礼貌抓取车系详情页（robots 已由榜单页确认；逐页限频）。返回 (rows, errors)。"""
    rows: list[dict] = []
    errors: list[str] = []
    for series_id in series_ids:
        url = f"{BASE_URL}/{series_id}/"
        try:
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", "replace")
            rows.append(parse_series_page(html, series_id))
        except Exception as err:  # noqa: BLE001
            errors.append(f"{series_id}: {type(err).__name__}")
        time.sleep(sleep_seconds)
    return rows, errors
