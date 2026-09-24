"""外部来源的「来源页」URL：把（来源名 + 外部 id）确定性地还原成可核对的原页面链接。

用途：详情页在**没有**官方车型页链接（`VehicleSeries.official_page_url`）时，给用户一个
「查看数据来源」入口——如实标注为来源方门户页面，**不冒充品牌官网**。

纪律：
- 只收录能由「来源名 + 外部 id」确定性拼出、且抓取链路实际使用过的模板；
- 未知来源、缺外部 id、外部 id 形态可疑一律返回 `None`——宁可没有链接，也不猜 URL。
"""
from __future__ import annotations

from typing import Callable

from app.sources.autohome import AUTOHOME_SOURCE_NAME, series_page_url

# 来源名 → URL 构造器。来源名与 `sources.name` 一致（导入时由载荷写入，见 app/sources/importer.py），
# 常量定义在 app/sources/autohome.py；抓取侧对应的 URL 形态见 app/sources/autohome_sku.py 与
# tools/fetch_autohome_*.py（改版式时两边都要改——目前是已知耦合，未提取共享常量）。
_PAGE_URL_BUILDERS: dict[str, Callable[[str], str | None]] = {
    AUTOHOME_SOURCE_NAME: series_page_url,
}


def source_page_url(source_name: str | None, external_id: str | None) -> str | None:
    """按来源名与外部车系 id 生成来源页 URL；无已知模板或 id 形态可疑时返回 None。"""
    if not source_name or not external_id:
        return None
    builder = _PAGE_URL_BUILDERS.get(source_name)
    return builder(external_id) if builder else None
