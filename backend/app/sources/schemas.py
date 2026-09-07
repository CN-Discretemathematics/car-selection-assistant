"""来源 API Schema。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    source_type: str
    url: str | None = None
    verified_status: str
    credibility: str
    last_verified_at: datetime | None = None
