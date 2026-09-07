"""汽车之家 SKU（款型/参数配置）数据接入。

数据来源（均为门户公开页面/接口，条款已由项目所有者确认允许）：
- 品牌车系索引：https://www.autohome.com.cn/grade/carhtml/{A-Z}.html
  （品牌、车系名、车系 ID、指导价区间）
- 参数配置接口：https://car-web-m.autohome.com.cn/car/param/getParamConf?site=1&seriesid=N
  （移动端「参数配置」页同源接口；返回 conditionlist / paramitems / configitems /
  specinfo.specitems，含全部在产款型与每款型的参数配置值）

合规：自定义 UA、每请求限频 ≥1 秒、来源标注
industry_data（公开行业数据）且 verified_status=unverified。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from app.sources.fetcher import DEFAULT_USER_AGENT

INDEX_URL_TEMPLATE = "https://www.autohome.com.cn/grade/carhtml/{letter}.html"
SKU_API = "https://car-web-m.autohome.com.cn/car/param/getParamConf"
SKU_PAGE_TEMPLATE = "https://car.m.autohome.com.cn/config/series/{seriesid}.html"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# 配置表空值符号：不写入事实表
SKIP_VALUES = {"", "-", "—", "--", "暂无数据"}
# specstatus==20 为在售（其余：即将上市/停售等，均记为 off_sale）
ON_SALE_SPEC_STATUS = 20

_YEAR_PREFIX_RE = re.compile(r"^\d{4}款\s*")
_DL_RE = re.compile(r'<dl id="(\d+)"[^>]*>(.*?)</dl>', re.S)
_BRAND_RE = re.compile(
    r"<dt>.*?<img[^>]+src=\"([^\"]+)\".*?<div><a[^>]*>([^<]+)</a></div></dt>", re.S
)
_LI_RE = re.compile(
    r"<li id=\"s(\d+)\">\s*<h4[^>]*><a[^>]+href=['\"][^'\"]*/(\d+)/[^'\"]*['\"]>([^<]+)</a></h4>"
    r"\s*<div>指导价：<a[^>]*>([^<]*)</a></div>",
    re.S,
)


# ── 品牌车系索引页 ────────────────────────────────────────────────

def parse_series_index(html: str) -> dict:
    """解析 grade/carhtml 索引页 → 品牌块与车系列表。"""
    brands: list[dict] = []
    for dl in _DL_RE.finditer(html):
        brand_id, block = dl.group(1), dl.group(2)
        brand_match = _BRAND_RE.search(block)
        name = (brand_match.group(2) or "").strip() if brand_match else ""
        logo = (brand_match.group(1) or "").strip() if brand_match else ""
        if not name:
            continue
        series: list[dict] = []
        for li in _LI_RE.finditer(block):
            series.append(
                {
                    "external_id": li.group(2),
                    "name": (li.group(3) or "").strip(),
                    "price_note": normalize_price_note(li.group(4) or ""),
                }
            )
        if series:
            brands.append(
                {
                    "brand_id": brand_id,
                    "name": name,
                    "logo": "https:" + logo if logo.startswith("//") else logo,
                    "series": series,
                }
            )
    return {"brands": brands}


def normalize_price_note(text: str) -> str | None:
    """把索引页指导价文本（如「16.59-20.99万」）归一化为「x万元」；无价格返回 None。"""
    t = (text or "").strip()
    if not t or "暂无" in t or "停售" in t:
        return None
    if t.endswith("万元"):
        return t
    if t.endswith("万"):
        return t + "元"
    return t


def decode_autohome_html(raw: bytes, content_type: str = "") -> str:
    """按响应 charset 解码门户页面（老 PC 页可能为 GBK，UTF-8 失败回退 gb18030）。

    任一候选产生替换字符（�）即视为解码失败抛出，避免静默损坏品牌/车系名。
    """
    candidates: list[str] = []
    m = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    if m and m.group(1).lower() not in candidates:
        candidates.append(m.group(1).lower())
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\ufffd" not in text:
            return text
    raise ValueError("页面解码失败（存在替换字符），可能为未知编码")


def fetch_series_index(letter: str) -> tuple[str, str]:
    """抓取一个字母的品牌车系索引页 → (html, url)。"""
    url = INDEX_URL_TEMPLATE.format(letter=letter.upper())
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = decode_autohome_html(resp.read(), resp.headers.get("Content-Type") or "")
    return html, url


# ── 参数配置接口（SKU） ──────────────────────────────────────────

def fetch_sku_config(series_id: str) -> dict:
    """抓取一个车系的参数配置（getParamConf）并解析为结构化字典。"""
    params = urllib.parse.urlencode({"site": 1, "seriesid": series_id})
    req = urllib.request.Request(
        SKU_API + "?" + params,
        headers={
            "User-Agent": MOBILE_UA,
            "Referer": SKU_PAGE_TEMPLATE.format(seriesid=series_id),
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    data = json.loads(raw)
    if data.get("returncode") not in (0, "0"):
        raise ValueError(f"接口返回错误：{data.get('message')}")
    return parse_sku_config(data.get("result") or {})


def parse_sku_config(result: dict) -> dict:
    """把 getParamConf 的 result 解析为 {condition_index, variants, groups}。

    condition_index: conditionlist 的 typevalue → 数组下标（不依赖位置，防止顺序变化错配）；
    variants: 款型（specid/年款/款型名/价格/状态/筛选条件）。
    groups:   [{category, items: [{key, values: {specid: value}}]}]，
              values 为每个款型在该参数上的取值（多值以「 / 」连接）。
    """
    condition_index: dict[str, int] = {}
    for i, cond in enumerate(result.get("conditionlist") or []):
        typevalue = cond.get("typevalue")
        if typevalue:
            condition_index[str(typevalue)] = i

    variants: list[dict] = []
    for s in (result.get("specinfo") or {}).get("specitems") or []:
        condition = s.get("condition") or []
        price = s.get("noshowprice")
        try:
            price_num = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_num = None
        variants.append(
            {
                "specid": str(s.get("specid") or ""),
                "year": s.get("year"),
                "specname": (s.get("specname") or "").strip(),
                "price_cny": int(price_num) if price_num is not None and price_num > 0 else None,
                "status": "on_sale" if s.get("specstatus") == ON_SALE_SPEC_STATUS else "off_sale",
                "condition": condition,
            }
        )

    groups: list[dict] = []
    for group in (result.get("paramitems") or []) + (result.get("configitems") or []):
        category = (group.get("groupname") or group.get("itemtype") or "").strip()
        items: list[dict] = []
        for item in group.get("items") or []:
            key = (item.get("name") or "").strip()
            if not key:
                continue
            values: dict[str, str] = {}
            for entry in item.get("modelexcessids") or []:
                spec_id = str(entry.get("id") or "")
                value = str(entry.get("value") or "").strip()
                sub = str(entry.get("subvalue") or "").strip()
                if sub:
                    value = f"{value} {sub}" if value else sub
                if not value:
                    continue
                if spec_id in values:
                    values[spec_id] = f"{values[spec_id]} / {value}"
                else:
                    values[spec_id] = value
            if values:
                items.append({"key": key, "values": values})
        if items:
            groups.append({"category": category, "items": items})
    return {"condition_index": condition_index, "variants": variants, "groups": groups}


# ── 载荷构建 ─────────────────────────────────────────────────────

_UNIT_RE = re.compile(r"[（(]([^）)]{1,8})[)）]$")
_UNIT_MAP = {
    "km": "km", "毫米": "mm", "mm": "mm", "kw": "kW", "千瓦": "kW",
    "l": "L", "升": "L", "l/100km": "L/100km", "kg": "kg", "千克": "kg",
    "个": "个", "座": "座", "°": "°", "度": "°", "秒": "s", "s": "s",
    "万元": "万元", "元": "元",
}
_CYCLE_RE = re.compile(r"(CLTC|NEDC|WLTC)", re.IGNORECASE)


def parse_fact_unit_cycle(fact_key: str) -> tuple[str | None, str | None]:
    """从配置项键名解析单位与工况（评审 M5）：如「CLTC纯电续航里程(km)」
    → unit=km, cycle=CLTC；无括号单位时尝试常见单位后缀。"""
    unit: str | None = None
    cycle: str | None = None
    m = _CYCLE_RE.search(fact_key)
    if m:
        cycle = m.group(1).upper()
    u = _UNIT_RE.search(fact_key)
    if u:
        raw = u.group(1).lower()
        unit = _UNIT_MAP.get(raw)
    if unit is None:
        for key, value in _UNIT_MAP.items():
            if fact_key.lower().endswith(key):
                unit = value
                break
    return unit, cycle


_SEAT_RE = re.compile(r"([一二两三四五六七八九十\d]+(?:\s*[+＋、]\s*[一二两三四五六七八九十\d]+)*)\s*座")
_SEAT_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_cn_number(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    if raw in _SEAT_DIGITS:
        return _SEAT_DIGITS[raw]
    return None


def _extract_seat_number(text: str) -> int | None:
    """解析座位数：支持「5」「5座」「七座」「2+2+3座」（求和）等形式（复审 M-M6-1）。"""
    m = _SEAT_RE.search(text or "")
    if m:
        parts = re.split(r"[+＋、]", m.group(1))
        total = 0
        for part in parts:
            n = _parse_cn_number(part.strip())
            if n is None:
                return None
            total += n
        return total or None
    stripped = (text or "").strip()
    if stripped.isdigit():
        return int(stripped)  # condition.seatcount 可能是纯数字「5」
    return None

def _body_from_condition(value: str) -> str | None:
    if "SUV" in value:
        return "suv"
    if "MPV" in value:
        return "mpv"
    if "皮卡" in value:
        return "pickup"
    if any(k in value for k in ("厢车", "轿车", "掀背")):
        return "sedan"
    return None


# 款型名能源标识词（大写比较）。顺序＝判定优先级：增程 > 插混 > 油混 > 纯电。
# 评审 P1（用户实测反馈）：补齐厂商自有命名——吉利雷神 EM-i（插混）、领克 EM-P（插混）、
# 丰田双擎E+（插混）、日产 e-POWER（油混）、雷神 Hi·P（插混）/ Hi·F（油混）。
# 此前「银河星耀8 225km EM-i」「星耀7 220km 四驱远航版」不含任何标识词，
# 落到「排量非新能源 → ICE」，插混被标成燃油，导致「要燃油车却推插混」。
_EREV_NAME_TOKENS = ("增程", "EREV", "REEV")
# 注意：只收「EM-I / EM-P」带连字符的写法——无连字符的 "EMI"/"EMP" 会命中
# "Premium"（实测：丰田bZ3「616km 长续航 Premium BEV」、库斯途「380TGDi LUX Premium」
# 会被误判成插混），已剔除。
_PHEV_NAME_TOKENS = (
    "DM", "DMI", "EM-I", "EM-P", "PHEV", "插混", "插电", "双擎E+", "HI·P",
)
_HEV_NAME_TOKENS = (
    "双擎", "油混", "轻混", "HEV", "混动", "E-POWER", "EPOWER", "HI·F", "智擎", "锐·混",
)
_BEV_NAME_TOKENS = ("EV", "纯电")
# 有发动机时的细分阈值：纯电续航 ≥50km 判插混（国标新能源口径），
# 否则只要有纯电续航或 ≥1kWh 电池判油混（HEV 电池普遍 1~2kWh，
# 燃油车启动电池通常以 Ah 标注、不写 kWh，故按 1kWh 设门槛避免误伤）。
PHEV_MIN_EV_RANGE_KM = 50.0
HEV_MIN_BATTERY_KWH = 1.0
NEW_ENERGY_DISPLACEMENT = "新能源"
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
# 电池容量以 Ah 标注的是启动电池（燃油车也有），不参与 kWh 判定
_BATTERY_AH_RE = re.compile(r"ah", re.I)
# 发动机类事实键：判定「真的带发动机」的证据来源
_ENGINE_KEY_RE = re.compile(r"(发动机|排量|气缸|进气形式|燃油标号)")


def normalize_displacement(value: str | None) -> str:
    """排量条件归一化。

    汽车之家给纯电款型的排量常写「0 / 0.0 / 0L」，这类占位值等价于「无发动机」，
    必须清空——否则会被当成燃油/插混证据（实测：大家7「527km 草原长续航版」
    排量 0、小鹏G6「625 Max」排量 0，被误判成 PHEV/EREV）。
    「新能源」原样返回（它本身就是无发动机的口径标记）。
    """
    text = (value or "").strip()
    if not text or text == NEW_ENERGY_DISPLACEMENT:
        return text
    match = _NUMBER_RE.search(text)
    if match and float(match.group(1)) == 0:
        return ""
    return text


def _fact_key_value(fact) -> tuple[str, str]:
    """同时兼容导入载荷 dict（fact_key/value）与 ORM SpecFact（fact_key/fact_value）。"""
    if isinstance(fact, dict):
        key = str(fact.get("fact_key") or "")
        value = fact.get("value", fact.get("fact_value"))
    else:
        key = str(getattr(fact, "fact_key", "") or "")
        value = getattr(fact, "fact_value", None)
    return key, "" if value is None else str(value)


def ev_evidence(facts) -> tuple[float, float]:
    """从参数事实提取 (纯电续航 km, 电池能量 kWh)，用于「有发动机但不是燃油车」的判定。

    入参既支持导入载荷的 dict，也支持 ORM SpecFact
    （tools/fix_energy_labels.py 复算历史数据时用）；无相关事实时返回 (0.0, 0.0)。
    """
    ev_range = 0.0
    battery = 0.0
    for fact in facts:
        key, value = _fact_key_value(fact)
        if not value or value.strip() in SKIP_VALUES:
            continue
        match = _NUMBER_RE.search(value)
        if not match:
            continue
        number = float(match.group(1))
        if "纯电续航" in key:
            ev_range = max(ev_range, number)
        elif ("电池能量" in key or "电池容量" in key) \
                and not _BATTERY_AH_RE.search(key) \
                and not _BATTERY_AH_RE.search(value.strip()):
            # 评审 m4：Ah 既可能写在键名（「电池容量(Ah)」）也可能写在取值（「60Ah」），
            # 两处都要排除——启动电池不是动力电池，否则燃油车会被判成油混
            battery = max(battery, number)
    return ev_range, battery


def has_engine_evidence(facts, displacement: str = "") -> bool:
    """是否「真的带发动机」：归一化后有实际排量，或有发动机类事实且取值有效。

    评审 M4：排量事实的**取值**必须参与判定——「0」是纯电占位，「新能源」「无」
    等非数值同样不代表发动机；否则存有「排量(L)=新能源」的纯电款型会被判成
    带发动机，再叠加纯电续航 ≥50km 就误判成插混。
    """
    disp = normalize_displacement(displacement)
    if disp and disp != NEW_ENERGY_DISPLACEMENT:
        return True
    for fact in facts:
        key, value = _fact_key_value(fact)
        if not _ENGINE_KEY_RE.search(key) or not value or value.strip() in SKIP_VALUES:
            continue
        if "排量" in key:
            match = _NUMBER_RE.search(value)
            if not match or float(match.group(1)) <= 0:
                continue
        return True
    return False


def classify_variant_energy(specname: str, displacement: str, has_engine: bool,
                            series_energy_types: list[str] | None,
                            ev_range_km: float = 0.0, battery_kwh: float = 0.0) -> str:
    """按款型名/排量条件/发动机事实/电驱证据/车系能源并集推断 SKU 能源类型。

    判定顺序：款型名标识词（厂商明确命名，最权威）→ 电驱证据（带发动机时细分
    插混/油混，避免「有排量就是燃油」）→ 排量/发动机事实 → 车系能源并集兜底。
    `ev_range_km`/`battery_kwh` 由 ev_evidence() 从参数页事实提取；
    `displacement` 会先经 normalize_displacement() 归一（「0」视为无发动机）。
    """
    name = (specname or "").upper()
    if any(k in name for k in _EREV_NAME_TOKENS):
        return "EREV"
    if any(k in name for k in _PHEV_NAME_TOKENS):
        return "PHEV"
    if any(k in name for k in _HEV_NAME_TOKENS):
        return "HEV"
    if any(k in name for k in _BEV_NAME_TOKENS):
        return "BEV"

    disp = normalize_displacement(displacement)
    is_new_energy_slot = disp == NEW_ENERGY_DISPLACEMENT
    union = set(series_energy_types or [])
    has_combustion = bool(has_engine) or bool(disp and not is_new_energy_slot)

    if has_combustion:
        # 纯电续航达标 → 可上绿牌的插混/增程；车系并集只标了增程时按增程
        if ev_range_km >= PHEV_MIN_EV_RANGE_KM:
            if union and "PHEV" not in union and "EREV" in union:
                return "EREV"
            return "PHEV"
        # 有小电池/短纯电续航 → 不插电的油电混动（含 48V 轻混）
        if ev_range_km > 0 or battery_kwh >= HEV_MIN_BATTERY_KWH:
            return "HEV"
        # 无任何电驱证据 → 燃油（排量未知但确有发动机事实的房车/商用车同样按燃油）
        if not is_new_energy_slot:
            return "ICE"
        # 排量条件写明「新能源」却带发动机 → 插混/增程/油混，按车系并集细分
        for cand in ("EREV", "PHEV", "HEV"):
            if cand in union:
                return cand
        return "PHEV"

    # 无发动机证据、款型名也无标识词 → 纯电（增程/插混必然带发动机）
    return "BEV"


def _powertrain_label(energy_type: str, displacement: str) -> str:
    labels = {"BEV": "纯电动", "EREV": "增程式", "PHEV": "插电混动", "HEV": "油电混动"}
    if energy_type in labels:
        return labels[energy_type]
    return displacement or "燃油"


def build_sku_payload(brand_meta: dict, series_meta: dict, parsed: dict,
                      page_url: str) -> dict:
    """把一个车系的参数配置解析结果转换为 importer 兼容载荷（含 model_years/variants/facts）。"""
    series_name = series_meta.get("name") or ""
    series_energy_types = series_meta.get("energy_types") or []

    model_years: dict[str, dict] = {}
    cond_idx = parsed.get("condition_index") or {}
    i_disp = cond_idx.get("displacement", 1)
    i_body = cond_idx.get("cartype", 4)
    i_drive = cond_idx.get("drivemode", 5)
    i_seats = cond_idx.get("seatcount", 6)
    for v in parsed["variants"]:
        if not v["specid"] or not v["year"]:
            continue
        year_name = f"{v['year']}款"
        condition = v["condition"]
        # 排量条件先归一化：纯电款型的「0 / 0.0」等价于无发动机，
        # 也避免「0」被 _powertrain_label 当成动力形式文案
        displacement = normalize_displacement(
            condition[i_disp] if len(condition) > i_disp else ""
        )
        drivetrain = condition[i_drive] if len(condition) > i_drive else ""
        body = _body_from_condition(condition[i_body]) if len(condition) > i_body else None

        facts: list[dict] = []
        for group in parsed["groups"]:
            for item in group["items"]:
                value = item["values"].get(v["specid"])
                if value is None or value in SKIP_VALUES:
                    continue
                unit, cycle = parse_fact_unit_cycle(item["key"])
                facts.append(
                    {
                        "category": group["category"],
                        "fact_key": item["key"],
                        "value": value,
                        "unit": unit,
                        "cycle": cycle,
                        "page_or_section": "汽车之家参数配置页",
                    }
                )

        # 座位数写入结构化 fact（评审 M6）：Agent 乘客硬约束与检索可依赖真实数据；
        # 若参数组已自带「座位数」项则跳过，避免同键重复（复审 M-M6-1）
        seat_raw = condition[i_seats] if len(condition) > i_seats else ""
        seat_count = _extract_seat_number(seat_raw)
        has_seat_fact = any(
            f["category"] == "参数信息" and f["fact_key"] == "座位数(个)" for f in facts
        )
        if seat_count is not None and not has_seat_fact:
            facts.append(
                {
                    "category": "参数信息",
                    "fact_key": "座位数(个)",
                    "value": str(seat_count),
                    "unit": "个",
                    "page_or_section": "汽车之家参数配置页",
                }
            )

        # 「是否带发动机」与电驱证据统一走共享判定，保证入库口径 == 复算口径
        # （tools/fix_energy_labels.py）
        has_engine = has_engine_evidence(facts, displacement)
        ev_range_km, battery_kwh = ev_evidence(facts)
        energy_type = classify_variant_energy(
            v["specname"], displacement, has_engine, series_energy_types,
            ev_range_km=ev_range_km, battery_kwh=battery_kwh,
        )
        config_version = _YEAR_PREFIX_RE.sub("", v["specname"]) or v["specname"]
        year = model_years.setdefault(
            year_name,
            {"year_name": year_name, "launch_status": "discontinued", "variants": []},
        )
        year["variants"].append(
            {
                "display_name": v["specname"],
                "config_version": config_version,
                "powertrain": _powertrain_label(energy_type, displacement),
                # drivetrain 为 NOT NULL 业务键字段：缺失时以「未标注」占位（展示层有
                # MISSING_LABEL 约定；改为可空需迁移，随下一次数据管线迭代处理）
                "drivetrain": drivetrain or "未标注",
                "energy_type": energy_type,
                "body_type": body,
                "status": v["status"],
                "price_cny": v["price_cny"],
                "facts": facts,
            }
        )
        if v["status"] == "on_sale":
            year["launch_status"] = "on_sale"

    # 车系级字段：能源类型取款型并集；车身类型取在售款型众数
    energy_union: list[str] = []
    for y in model_years.values():
        for v in y["variants"]:
            if v["energy_type"] not in energy_union:
                energy_union.append(v["energy_type"])
    bodies = [v["body_type"] for y in model_years.values() for v in y["variants"]
              if v.get("body_type") and v["status"] == "on_sale"]
    body_type = max(set(bodies), key=bodies.count) if bodies else None

    series_cfg: dict = {
        "brand": brand_meta["name"],
        "name": series_name,
        "external_id": str(series_meta["external_id"]),
    }
    if body_type:
        series_cfg["body_type"] = body_type
    if energy_union:
        series_cfg["energy_types"] = energy_union
    elif series_energy_types:
        series_cfg["energy_types"] = series_energy_types
    price_note = series_meta.get("price_note")
    if price_note:
        series_cfg["price_range_note"] = price_note
    if series_meta.get("thumbnail_url"):
        series_cfg["thumbnail_url"] = series_meta["thumbnail_url"]
    if model_years:
        series_cfg["model_years"] = list(model_years.values())

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
                "name": brand_meta["name"],
                "brand_type": brand_meta.get("brand_type", "other_fuel"),
                "inclusion_reason": brand_meta.get(
                    "inclusion_reason", "汽车之家品牌车系索引（品牌分类待确认）"
                ),
            }
        ],
        "series": [series_cfg],
        "sales": [],
    }
