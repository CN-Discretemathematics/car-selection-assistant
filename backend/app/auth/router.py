"""认证与收藏接口。

- POST /auth/register      邮箱注册（发送验证码；开发模式可选回显）
- POST /auth/login         邮箱登录（等同发送验证码，幂等）
- POST /auth/verify-code   验证码校验 → 返回令牌
- GET  /me                 当前用户
- GET  /me/favorites       收藏列表
- PUT  /me/favorites/{vehicle_id}     收藏（series | variant）
- DELETE /me/favorites/{vehicle_id}   取消收藏

验证码发送在本地开发为控制台输出（ConsoleSender）；生产接入邮件服务（SMTP 配置）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.schemas import AuthOut, FavoriteOut, RegisterIn, UserOut, VerifyIn
from app.auth.security import get_code_store, get_rate_limiter, get_token_store
from app.catalog import services as catalog
from app.common.config import get_settings
from app.common.database import get_session
from app.common.errors import bad_request, not_found
from app.common.models import Brand, Favorite, User, VehicleSeries, VehicleVariant

logger = logging.getLogger("car-selection.auth")

router = APIRouter(tags=["auth"])


def unauthorized(detail: str):
    from fastapi import HTTPException

    return HTTPException(status_code=401, detail=detail)


def _send_code(email: str, code: str) -> None:
    """验证码投递：SMTP 已配置走邮件；否则开发模式日志（生产接入前明确提示）。"""
    from app.common.email_sender import EmailSender

    EmailSender().send_code(email, code)


@router.post("/auth/register", response_model=AuthOut)
def register(payload: RegisterIn, db: Session = Depends(get_session)) -> AuthOut:
    email = payload.email.strip().lower()
    return _issue_code(db, email, is_registration=True)


@router.post("/auth/login", response_model=AuthOut)
def login(payload: RegisterIn, db: Session = Depends(get_session)) -> AuthOut:
    """登录：注册即登录语义——未注册邮箱同样发验证码并自动建号（评审 L5：
    不区分「是否注册」以避免邮箱枚举）。"""
    email = payload.email.strip().lower()
    return _issue_code(db, email, is_registration=True)


def _issue_code(db: Session, email: str, is_registration: bool) -> AuthOut:
    limiter = get_rate_limiter()
    if not limiter.hit(f"auth-code:{email}"):
        raise bad_request("验证码请求过于频繁，请稍后再试（每小时最多 5 次）。")

    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # 并发注册竞态：唯一约束兜底，读取既有账号
            user = db.scalar(select(User).where(User.email == email))
        db.refresh(user)

    code = get_code_store().issue(email)
    # 评审 P2：SMTP 投递失败不得 500（账号/验证码已落库，用户仍可通过重发继续）
    try:
        _send_code(email, code)
    except Exception:  # noqa: BLE001
        from fastapi import HTTPException

        raise HTTPException(
            status_code=502,
            detail="验证码邮件发送失败，请稍后重试或重新获取。",
        )
    settings = get_settings()
    return AuthOut(
        token="",
        user=UserOut(
            id=user.id,
            email=user.email,
            phone=user.phone,
            status=user.status,
            created_at=user.created_at,
        ),
        dev_code=code if settings.dev_echo_codes else None,
    )


@router.post("/auth/verify-code", response_model=AuthOut)
def verify_code(payload: VerifyIn, db: Session = Depends(get_session)) -> AuthOut:
    email = payload.email.strip().lower()
    if not get_code_store().verify(email, payload.code):
        raise bad_request("验证码错误或已过期，请重新获取。")
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        raise not_found("该邮箱尚未注册。")
    token = get_token_store().issue(user.id)
    return AuthOut(
        token=token,
        user=UserOut(
            id=user.id,
            email=user.email,
            phone=user.phone,
            status=user.status,
            created_at=user.created_at,
        ),
    )


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> User:
    """Bearer 令牌鉴权依赖。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise unauthorized("未登录，请先登录。")
    token = authorization[len("Bearer "):].strip()
    user_id = get_token_store().resolve(token)
    if user_id is None:
        raise unauthorized("登录已过期，请重新登录。")
    user = db.get(User, user_id)
    if user is None or user.status != "active":
        raise unauthorized("账号不可用。")
    return user


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=user.id, email=user.email, phone=user.phone, status=user.status, created_at=user.created_at)


@router.delete("/me")
def delete_me(
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    """注销账号：禁用账号并撤销当前令牌（收藏数据保留在库中，账号不再可登录）。"""
    user.status = "disabled"
    db.commit()
    if authorization and authorization.startswith("Bearer "):
        get_token_store().revoke(authorization[len("Bearer "):].strip())
    return {"status": "deleted"}


@router.get("/me/favorites", response_model=list[FavoriteOut])
def list_favorites(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[FavoriteOut]:
    favorites = db.scalars(
        select(Favorite).where(Favorite.user_id == user.id).order_by(Favorite.created_at.desc())
    ).all()
    return [_favorite_out(db, f) for f in favorites]


@router.put("/me/favorites/{vehicle_id}", response_model=FavoriteOut, status_code=201)
def add_favorite(
    vehicle_id: int,
    user: User = Depends(get_current_user),
    kind: str = Query(default="series", pattern="^(series|variant)$"),
    db: Session = Depends(get_session),
) -> FavoriteOut:
    _validate_vehicle(db, vehicle_id, kind)

    existing = db.scalar(
        select(Favorite).where(
            Favorite.user_id == user.id,
            Favorite.vehicle_id == vehicle_id,
            Favorite.kind == kind,
        )
    )
    if existing is not None:
        return _favorite_out(db, existing)
    favorite = Favorite(user_id=user.id, vehicle_id=vehicle_id, kind=kind)
    db.add(favorite)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # 并发重复收藏竞态：唯一约束兜底，返回已有收藏
        existing = db.scalar(
            select(Favorite).where(
                Favorite.user_id == user.id,
                Favorite.vehicle_id == vehicle_id,
                Favorite.kind == kind,
            )
        )
        return _favorite_out(db, existing)
    db.refresh(favorite)
    return _favorite_out(db, favorite)


@router.delete("/me/favorites/{vehicle_id}")
def remove_favorite(
    vehicle_id: int,
    user: User = Depends(get_current_user),
    kind: str = Query(default="series", pattern="^(series|variant)$"),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    favorite = db.scalar(
        select(Favorite).where(
            Favorite.user_id == user.id,
            Favorite.vehicle_id == vehicle_id,
            Favorite.kind == kind,
        )
    )
    if favorite is None:
        raise not_found("收藏不存在。")
    db.delete(favorite)
    db.commit()
    return {"status": "deleted"}


def _validate_vehicle(db: Session, vehicle_id: int, kind: str) -> None:
    if kind == "series":
        series = catalog.get_series(db, vehicle_id)
        if series is None or series.active_status != "active":
            raise not_found(f"车型系列不存在或已停售：{vehicle_id}")
    else:
        variant = catalog.get_variant(db, vehicle_id)
        if variant is None or variant.status != "on_sale":
            raise not_found(f"SKU 不存在或已停售：{vehicle_id}")


def _favorite_out(db: Session, favorite: Favorite) -> FavoriteOut:
    name = brand_name = None
    price_cny = None
    series_id = None
    if favorite.kind == "series":
        series = catalog.get_series(db, favorite.vehicle_id)
        if series is not None:
            series_id = series.id
            name = series.name
            brand = db.get(Brand, series.brand_id)
            brand_name = brand.name if brand else None
            price_min, _ = catalog.series_price_range(db, series.id)
            price_cny = float(price_min) if price_min is not None else None
    else:
        variant = catalog.get_variant(db, favorite.vehicle_id)
        if variant is not None:
            series_id = variant.series_id
            name = variant.display_name
            series = db.get(VehicleSeries, variant.series_id)
            brand = db.get(Brand, series.brand_id) if series else None
            brand_name = brand.name if brand else None
            price = catalog.variant_current_price(db, variant.id)
            price_cny = float(price.price_cny) if price else None
    return FavoriteOut(
        vehicle_id=favorite.vehicle_id,
        kind=favorite.kind,
        series_id=series_id,
        name=name,
        brand_name=brand_name,
        price_cny=price_cny,
        created_at=favorite.created_at,
    )
