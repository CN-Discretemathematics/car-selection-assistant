"""FastAPI 公共后端入口。

所有前端 API、SSE 和业务编排的唯一入口。
前端不得直连 DeepSeek、Milvus 或 PostgreSQL。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.agent.router import router as agent_router
from app.admin.rag_router import router as admin_rag_router
from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.brands.router import router as brands_router
from app.common.config import get_settings
from app.common.database import create_all, get_session_factory
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
    # 安全评审：交互式文档默认关闭（会枚举管理端点），开发/内网用 DOCS_ENABLED=true 开启
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
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


@app.get(f"{api}/ready", tags=["health"])
def ready(response: Response) -> dict:
    """就绪探针（容器 healthcheck 用）：真实探测依赖，数据库不可用返回 503。

    与 /health（存活探针，恒定 200）区分——安全评审 #24：此前 healthcheck 只打 /health，
    数据库挂掉后容器仍被判定健康、web 照常启动。Redis 为可选依赖（不可用时按设计
    降级为进程内存储），只报告状态、不判失败。
    """
    from sqlalchemy import text

    checks: dict[str, str] = {}
    try:
        with get_session_factory()() as db:
            db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as err:  # noqa: BLE001 - 探针只如实报告状态
        checks["database"] = f"error: {type(err).__name__}"
    try:
        from app.common.redis_client import get_redis

        client = get_redis()
        checks["redis"] = "ok" if client is not None else "unavailable(fallback to in-process)"
    except Exception as err:  # noqa: BLE001
        checks["redis"] = f"error: {type(err).__name__}"
    ready_ok = checks["database"] == "ok"
    response.status_code = 200 if ready_ok else 503
    return {"status": "ready" if ready_ok else "not-ready", "checks": checks}
