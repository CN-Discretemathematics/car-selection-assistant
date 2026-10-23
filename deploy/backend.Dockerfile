# carSelection 后端镜像（FastAPI + uvicorn）
# 构建：docker build -f deploy/backend.Dockerfile -t carsel-api .
# 运行依赖环境变量：见 backend/.env.example（生产经 .env 注入，不入仓库）
FROM python:3.12-slim

# 2026-10-05：构建时注入源码提交号，供 /api/v1/version 回报。
# 目的：让「服务器跑的到底是不是 main 最新」**从外部可验**，而不必 SSH 上服务器
# 去读 /var/lib/carsel/deployed-main.sha。此前只有 /health，而它返回的是
# 写死的 settings.app_version（0.1.0），无论部署了哪个提交都不变——**看起来
# 健康却无法证明是最新版本**，这正是本次事故的形状。
# 未注入时留空，接口返回 "unknown" 而不是编一个值。
ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME} \
    PYTHONUNBUFFERED=1 \
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

# ── 非 root 运行（README 已知待办「容器非 root 运行」，2026-10-22 落地）────
#
# 为什么：容器以 root 跑时，一旦发生容器逃逸，攻击者拿到的就是宿主上的 root。
# compose 侧此前已有 no-new-privileges / cap_drop ALL / 内存与进程数限制，
# 但**用户身份**这一层是空的——三个加固里最直接的一层反而没做。
#
# uid 固定为 10001（不用随机 uid）：compose 把宿主 `backend/.tmp` 目录
# bind-mount 进容器，而管理审计日志（admin-audit.log）、dense 水位标记、
# 销量导入 flag 都写在这个目录 ⇒ 宿主侧要按**同一个 uid** 授权，
# 否则容器起来就写不进去。授权动作在 deploy/carsel-deploy.sh 里（root 执行 chown）。
#
# 应用侧不需要 root 的任何能力：监听 8000（>1024）、写一个已授权的目录、
# 连数据库与 Redis——全部无需特权。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin carsel \
 && mkdir -p /srv/carsel/backend/.tmp \
 && chown -R carsel:carsel /srv/carsel/backend
USER carsel

# 默认监听 8000；启动时先跑迁移（幂等）再起服务。
# 注（评审 M8）：认证/会话在 Redis 不可用时回退进程内存储，多 worker 会分裂脑——
# 默认单 worker；多 worker 部署必须保证 REDIS_URL 可用。
# --proxy-headers + --forwarded-allow-ips=127.0.0.1（安全评审 2026-09-13）：让应用读到
# nginx 传来的真实客户端 IP——否则限流中间件按 request.client.host 计数会退化成
# 「所有请求同一个桶」，按 IP 限流形同虚设。只信任本机 nginx（不可用 * ，否则可伪造）。
EXPOSE 8000
CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1 --workers ${UVICORN_WORKERS:-1}"]
