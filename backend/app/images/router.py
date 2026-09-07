"""图片代理接口：GET /api/v1/images/proxy?url=<白名单图片地址>。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter
from fastapi.responses import Response

from app.common.errors import bad_request, not_found
from app.common.images import MAX_IMAGE_BYTES, is_allowed_image_url
from app.sources.fetcher import DEFAULT_USER_AGENT

router = APIRouter(tags=["images"])


@router.get("/images/proxy")
def image_proxy(url: str) -> Response:
    if not is_allowed_image_url(url):
        raise bad_request("图片地址不在允许的来源白名单内")
    try:
        resp = httpx.get(
            url,
            timeout=15.0,
            trust_env=False,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://www.autohome.com.cn/"},
        )
    except httpx.HTTPError as err:  # noqa: BLE001
        raise not_found(f"图片源不可达：{type(err).__name__}") from err
    if resp.status_code != 200:
        raise not_found(f"图片源返回 {resp.status_code}")
    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise bad_request("该地址不是图片资源")
    body = resp.content
    if len(body) > MAX_IMAGE_BYTES:
        raise bad_request("图片超过大小限制")
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
