"""账户认证与收藏接口测试。"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import CodeStore
from app.common.models import Favorite, User
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _register(client: TestClient, email: str) -> str:
    resp = client.post("/api/v1/auth/register", json={"email": email})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dev_code"], "测试环境应回显验证码（DEV_ECHO_CODES=true）"
    return body["dev_code"]


def _login(client: TestClient, email: str, code: str) -> str:
    resp = client.post("/api/v1/auth/verify-code", json={"email": email, "code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    assert token
    return token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_register_login_me_flow(client: TestClient, db_session: Session):
    email = "user1@example.com"
    code = _register(client, email)
    token = _login(client, email, code)

    me = client.get("/api/v1/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["email"] == email

    # 账号只存邮箱/状态，无额外隐私字段
    user = db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.phone is None


def test_login_creates_account_register_like(client: TestClient, db_session: Session):
    """登录即注册（评审 L5）：未注册邮箱登录不再返回 404（避免邮箱枚举），直接建号发码。"""
    from app.common.models import User

    email = "nobody@example.com"
    resp = client.post("/api/v1/auth/login", json={"email": email})
    assert resp.status_code == 200
    assert db_session.scalar(select(User).where(User.email == email)) is not None


def test_wrong_code_rejected(client: TestClient):
    email = "user2@example.com"
    _register(client, email)
    resp = client.post("/api/v1/auth/verify-code", json={"email": email, "code": "000000"})
    assert resp.status_code == 400


def test_code_store_expiry_and_attempts():
    store = CodeStore(ttl_seconds=300)
    email = "unit@example.com"
    code = store.issue(email)
    assert store.verify(email, "000000") is False

    # 尝试次数超限后即使正确验证码也被拒绝
    for _ in range(CodeStore.MAX_ATTEMPTS - 1):
        store.verify(email, "000000")
    assert store.verify(email, code) is False

    # 过期验证码被拒绝
    store2 = CodeStore(ttl_seconds=-1)  # 立即过期
    code2 = store2.issue(email)
    assert store2.verify(email, code2) is False


def test_code_store_expiry_and_attempts_guard_time():
    # TTL 过期路径（非负数 TTL 的常规验证）
    store = CodeStore(ttl_seconds=300)
    email = "ttl@example.com"
    code = store.issue(email)
    store._data[email].expires_at = time.time() - 1  # 手动置为已过期
    assert store.verify(email, code) is False


def test_rate_limit_code_requests(client: TestClient):
    email = "user3@example.com"
    for _ in range(5):
        assert client.post("/api/v1/auth/register", json={"email": email}).status_code == 200
    resp = client.post("/api/v1/auth/register", json={"email": email})
    assert resp.status_code == 400  # 每小时 5 次上限


def test_account_deletion(client: TestClient, db_session: Session):
    email = "user5@example.com"
    token = _login(client, email, _register(client, email))
    headers = _auth(token)

    deleted = client.delete("/api/v1/me", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"

    # 注销后令牌失效，无法继续访问
    assert client.get("/api/v1/me", headers=headers).status_code == 401
    user = db_session.scalar(select(User).where(User.email == email))
    assert user.status == "disabled"


def test_bad_token_rejected(client: TestClient):
    assert client.get("/api/v1/me", headers=_auth("invalid-token")).status_code == 401
    assert client.get("/api/v1/me").status_code == 401


def test_favorites_crud(client: TestClient, db_session: Session):
    source = make_source(db_session)
    brand = make_brand(db_session, name="收藏测试品牌", source=source)
    series = make_series(db_session, brand, name="收藏测试车系", source=source)
    year = make_year(db_session, series)
    variant = make_variant(db_session, series, year, config_version="标准版", price_cny="129800", source=source)
    db_session.commit()

    email = "user4@example.com"
    token = _login(client, email, _register(client, email))
    headers = _auth(token)

    # 收藏车系（kind 走查询参数，与 DELETE 一致）
    put = client.put(f"/api/v1/me/favorites/{series.id}", params={"kind": "series"}, headers=headers)
    assert put.status_code == 201
    assert put.json()["name"] == "收藏测试车系"
    assert put.json()["brand_name"] == "收藏测试品牌"

    # 幂等：重复收藏不产生重复行
    again = client.put(f"/api/v1/me/favorites/{series.id}", params={"kind": "series"}, headers=headers)
    assert again.status_code == 201
    assert len(db_session.scalars(select(Favorite)).all()) == 1

    # 收藏 SKU（响应带 series_id 供深链）
    put_v = client.put(f"/api/v1/me/favorites/{variant.id}", params={"kind": "variant"}, headers=headers)
    assert put_v.status_code == 201
    assert put_v.json()["price_cny"] == 129800.0
    assert put_v.json()["series_id"] == series.id

    listing = client.get("/api/v1/me/favorites", headers=headers).json()
    assert len(listing) == 2

    # 删除
    deleted = client.delete(f"/api/v1/me/favorites/{series.id}?kind=series", headers=headers)
    assert deleted.status_code == 200
    assert client.get("/api/v1/me/favorites", headers=headers).json()[0]["kind"] == "variant"
    assert client.delete(f"/api/v1/me/favorites/{series.id}?kind=series", headers=headers).status_code == 404

    # 无效车辆 / 非法 kind / 停售 SKU
    assert client.put("/api/v1/me/favorites/999999", params={"kind": "series"}, headers=headers).status_code == 404
    assert client.put(f"/api/v1/me/favorites/{series.id}", params={"kind": "bogus"}, headers=headers).status_code == 422
    off_sale = db_session.get(type(variant), variant.id)
    off_sale.status = "off_sale"
    db_session.commit()
    assert client.put(f"/api/v1/me/favorites/{variant.id}", params={"kind": "variant"}, headers=headers).status_code == 404

    # 未登录不可访问收藏
    assert client.get("/api/v1/me/favorites").status_code == 401
