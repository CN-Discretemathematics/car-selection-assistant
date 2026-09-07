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
