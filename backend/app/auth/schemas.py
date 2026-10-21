"""认证 API Schema。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_RE)


class VerifyIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_RE)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class MergeIdentityIn(BaseModel):
    """把某个**匿名身份**的数据并进当前账户。

    匿名 token 放**请求体**而不是请求头：请求头已经带着**账户令牌**了 ✓
    （两个令牌不能共用一个 `Authorization` 头 ✓）。
    """

    anonymous_token: str = Field(min_length=8, max_length=200)


class MergeIdentityOut(BaseModel):
    """合并结果。

    `token` 是**账户身份**的新令牌——**仅在新建时**有值（明文只给这一次 ✓，
    与 `/agent/identity` 同一约定 ✓）。
    """

    identity_id: int
    token: str | None = None
    memories_moved: int = 0
    feedbacks_moved: int = 0


class UserOut(BaseModel):
    id: int
    email: str | None = None
    phone: str | None = None
    status: str
    created_at: datetime


class AuthOut(BaseModel):
    token: str
    user: UserOut
    # 仅开发模式回显验证码（settings.dev_echo_codes=true）；生产恒为 None
    dev_code: str | None = None


class FavoriteOut(BaseModel):
    vehicle_id: int
    kind: str
    series_id: int | None = None  # variant 收藏用于深链到所属系列
    name: str | None = None
    brand_name: str | None = None
    price_cny: float | None = None
    created_at: datetime
