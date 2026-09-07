"""FastAPI 公共后端入口。

所有前端 API、SSE 和业务编排的唯一入口。
前端不得直连 DeepSeek、Milvus 或 PostgreSQL。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.router import router as agent_router
from app.admin.rag_router import router as admin_rag_router
from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.brands.router import router as brands_router
from app.common.config import get_settings
from app.common.database import create_all
from app.common.ratelimit import RateLimitMiddleware
from app.comparison.router import router as comparison_router
from app.images.router import router as images_router
from app.recommendation.router import router as recommendation_router
from app.sales.router import router as sales_router
from app.sources.router import router as sources_router
from app.vehicles.router import router as vehicles_router

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.auto_create_tables:
        create_all()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 本地开发基础限流（每 IP 每分钟 1000 次，健康检查豁免）；生产由 WAF/Redis 承担
app.add_middleware(RateLimitMiddleware, max_requests=1000, window_seconds=60)

api = settings.api_prefix
app.include_router(brands_router, prefix=api)
app.include_router(vehicles_router, prefix=api)
app.include_router(sales_router, prefix=api)
app.include_router(comparison_router, prefix=api)
app.include_router(recommendation_router, prefix=api)
app.include_router(images_router, prefix=api)
app.include_router(sources_router, prefix=api)
app.include_router(agent_router, prefix=api)
app.include_router(auth_router, prefix=api)
app.include_router(admin_router, prefix=api)
app.include_router(admin_rag_router, prefix=api)


@app.get(f"{api}/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name, "version": settings.app_version}
