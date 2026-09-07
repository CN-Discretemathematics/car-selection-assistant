"""来源接口：GET /sources/{source_id}。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.database import get_session
from app.common.errors import not_found
from app.common.models import Source
from app.sources.schemas import SourceOut

router = APIRouter(tags=["sources"])


@router.get("/sources/{source_id}", response_model=SourceOut)
def get_source(source_id: int, db: Session = Depends(get_session)) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise not_found(f"来源不存在：{source_id}")
    return source
