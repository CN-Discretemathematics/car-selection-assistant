# carSelection 前端镜像：Next.js 构建产物 + nginx（静态 + /api 反向代理）
# 构建：docker build -f deploy/frontend.Dockerfile -t carsel-web .
# 说明：Next.js App Router 动态路由（销量榜/详情页）需 node 运行时，
#       因此容器内同时跑 nginx（静态/代理）与 next start；也可以只用 node + nginx 主机反代。
FROM node:22-slim AS build
WORKDIR /srv/carsel/web
# 国内镜像源：npmmirror（pnpm 锁文件按 integrity 校验，registry 切换不影响 --frozen-lockfile）
ENV npm_config_registry=https://registry.npmmirror.com \
    COREPACK_NPM_REGISTRY=https://registry.npmmirror.com
# 构建期注入后端地址（评审 N1）：next.config 的 /api/v1 rewrite 在 build 阶段固化，
# 容器内浏览器端必须指向 api 服务；SSR 的 BACKEND_URL 在运行时仍可覆盖
ARG BACKEND_URL=http://api:8000
ENV BACKEND_URL=${BACKEND_URL}
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY web/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm run build

FROM node:22-slim
WORKDIR /srv/carsel/web
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build /srv/carsel/web/.next ./.next
COPY --from=build /srv/carsel/web/node_modules ./node_modules
COPY --from=build /srv/carsel/web/package.json ./package.json
COPY --from=build /srv/carsel/web/public ./public
EXPOSE 3000
CMD ["sh", "-c", "corepack enable && pnpm exec next start -p 3000"]
