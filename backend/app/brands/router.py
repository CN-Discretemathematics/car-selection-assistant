"""品牌接口：GET /brands。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brands.schemas import BrandOut
from app.common.database import get_session
from app.common.models import Brand

router = APIRouter(tags=["brands"])


@router.get("/brands", response_model=list[BrandOut])
def list_brands(db: Session = Depends(get_session)) -> list[Brand]:
    """返回纳入品牌注册表；默认只返回在售（active）品牌。"""
    stmt = select(Brand).where(Brand.active_status == "active").order_by(Brand.id)
    return list(db.scalars(stmt))
