"""统一 API 错误辅助。"""
from __future__ import annotations

from fastapi import HTTPException


def not_found(detail: str = "资源不存在") -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)
