# carSelection 后端镜像（PROJECT_PLAN.md 阶段 2「FastAPI 容器」）
# 构建：docker build -f deploy/backend.Dockerfile -t carsel-api .
# 运行依赖环境变量：见 backend/.env.example（生产经 .env 注入，不入仓库）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /srv/carsel/backend

# 先装依赖（层缓存友好）
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
COPY backend/app ./app
COPY backend/tools ./tools

# 默认监听 8000；启动时先跑迁移（幂等）再起服务。
# 注（评审 M8）：认证/会话在 Redis 不可用时回退进程内存储，多 worker 会分裂脑——
# 默认单 worker；多 worker 部署必须保证 REDIS_URL 可用。
EXPOSE 8000
CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-1}"]
