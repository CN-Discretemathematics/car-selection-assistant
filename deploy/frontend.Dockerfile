# carSelection 前端镜像：Next.js 构建产物 + nginx（静态 + /api 反向代理）
# 构建：docker build -f deploy/frontend.Dockerfile -t carsel-web .
# 说明：Next.js App Router 动态路由（销量榜/详情页）需 node 运行时，
#       因此容器内同时跑 nginx（静态/代理）与 next start；也可以只用 node + nginx 主机反代。
FROM node:22-slim AS build
WORKDIR /srv/carsel/web
# 国内镜像源：npmmirror。注意（2026-09-13 实测）：新版 pnpm 不认 npm_config_registry
# 环境变量——只设 ENV 时 174 次请求仍走 registry.npmjs.org（大陆节点不可达）导致构建
# 超时失败，必须把 registry 落到 .npmrc；COREPACK_NPM_REGISTRY 供 corepack 取 pnpm 本体。
ENV COREPACK_NPM_REGISTRY=https://registry.npmmirror.com
# 构建期注入后端地址（评审 N1）：next.config 的 /api/v1 rewrite 在 build 阶段固化，
# 容器内浏览器端必须指向 api 服务；SSR 的 BACKEND_URL 在运行时仍可覆盖
ARG BACKEND_URL=http://api:8000
ENV BACKEND_URL=${BACKEND_URL}
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable \
 && printf 'registry=https://registry.npmmirror.com\n' > /root/.npmrc \
 && pnpm install --frozen-lockfile
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
# 运行时直接调用镜像内的 next 二进制（node_modules 已随镜像带入）：
# 不用 corepack/pnpm——它们会在容器启动时联网下载 pnpm 本体，大陆节点不可达时
# 表现为「容器 Up 但 3000 无响应、日志空白」（2026-09-13 实测）
CMD ["node", "node_modules/next/dist/bin/next", "start", "-p", "3000"]
