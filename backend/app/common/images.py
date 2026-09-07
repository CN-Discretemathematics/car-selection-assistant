"""图片代理（§20 版权合规：只转发展示已授权来源的图片链接，不归档不转存）。

- 白名单域名（汽车之家图片 CDN 及子域）之外一律拒绝（防 SSRF）；
- 只允许 image/* 响应且限制大小；
- 返回相对路径 /api/v1/images/proxy?url=...，前端同源加载，
  规避浏览器侧 DNS/代理/广告拦截对第三方图床的阻断。
"""
from __future__ import annotations

from urllib.parse import quote, urlparse

IMAGE_HOST_ALLOWLIST = ("autoimg.cn",)  # 汽车之家图片 CDN（car2/car3 等子域）
MAX_IMAGE_BYTES = 8 * 1024 * 1024
PROXY_PATH = "/api/v1/images/proxy"


def is_allowed_image_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in IMAGE_HOST_ALLOWLIST)


def proxy_image_url(url: str | None) -> str | None:
    """把白名单图片地址改写为后端代理地址；其余来源保持原样（官方站点图片直链）。"""
    if not url:
        return None
    if not is_allowed_image_url(url):
        return url
    return f"{PROXY_PATH}?url={quote(url, safe='')}"
