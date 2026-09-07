"""图片代理接口与白名单测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.common.images import is_allowed_image_url, proxy_image_url


def test_proxy_image_url_helper():
    assert is_allowed_image_url("https://car3.autoimg.cn/x/y.png")
    assert is_allowed_image_url("https://car2.autoimg.cn/z.jpg")
    assert not is_allowed_image_url("https://evil.com/x.png")
    assert not is_allowed_image_url("https://autoimg.cn.evil.com/x.png")
    assert (
        proxy_image_url("https://car3.autoimg.cn/x/y.png")
        == "/api/v1/images/proxy?url=https%3A%2F%2Fcar3.autoimg.cn%2Fx%2Fy.png"
    )
    # 非白名单来源（如官方站点图片）保持原样直链
    assert proxy_image_url("https://brand.example.com/official.png") == "https://brand.example.com/official.png"
    assert proxy_image_url(None) is None


def test_image_proxy_endpoint(client: TestClient, monkeypatch):
    from app.images import router as images_router

    class FakeResp:
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        content = b"FAKEJPEG"

    monkeypatch.setattr(images_router.httpx, "get", lambda *a, **k: FakeResp())
    resp = client.get("/api/v1/images/proxy", params={"url": "https://car3.autoimg.cn/x/y.png"})
    assert resp.status_code == 200
    assert resp.content == b"FAKEJPEG"
    assert resp.headers["content-type"] == "image/jpeg"
    assert "max-age" in resp.headers["cache-control"]

    # 白名单之外一律 400（防 SSRF）
    resp = client.get("/api/v1/images/proxy", params={"url": "https://evil.com/x.png"})
    assert resp.status_code == 400


def test_home_cards_use_proxy_thumbnail(client: TestClient, db_session, monkeypatch):
    """首页接口输出的汽车之家缩略图应改写为同源代理地址。"""
    from sqlalchemy.orm import Session

    from app.common.models import MonthlySales
    from tests.seed import make_brand, make_series, make_source, make_year

    db: Session = db_session
    source = make_source(db, name="汽车之家")
    brand = make_brand(db, name="测试品牌", source=source)
    series = make_series(db, brand, name="Model Y", source=source)
    series.thumbnail_url = "https://car3.autoimg.cn/x.png"
    make_year(db, series)
    db.add(
        MonthlySales(
            series_id=series.id, month="2026-07", sales_type="portal",
            sales_count=38654, source_id=source.id,
        )
    )
    db.commit()

    cards = client.get("/api/v1/home", params={"month": "2026-07"}).json()
    assert cards, "首页应有卡片"
    assert cards[0]["thumbnail_url"].startswith("/api/v1/images/proxy?url=")
