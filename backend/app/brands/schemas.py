"""品牌 API Schema。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class BrandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    aliases: list[str] = []
    parent_company: str | None = None
    brand_type: str
    consumer_sales: bool = True
    official_site: str | None = None
    active_status: Literal["active", "inactive"] = "active"
    inclusion_reason: str | None = None
    last_verified_at: datetime | None = None
