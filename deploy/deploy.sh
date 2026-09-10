#!/usr/bin/env bash
# carSelection 一键部署（非容器，Ubuntu/Debian 轻量服务器 2C4G）
# 用法（root 或 sudo）：bash deploy/deploy.sh
# 前置：backend/.env 已按 .env.example 填好生产值；域名已解析并备案
set -euo pipefail

APP_ROOT=/srv/carsel
APP_USER=carsel

echo "[1/7] 安装系统依赖…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx nodejs npm corepack 2>/dev/null || \
  apt-get install -y -qq python3 python3-venv python3-pip nginx
# Node 22（Next 15 要求 ≥18.18；Ubuntu 24.04 自带 node 18/20，够用则跳过）
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
fi

echo "[2/7] 创建运行用户与目录…"
id -u "$APP_USER" >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_ROOT"
chown -R "$APP_USER:$APP_USER" "$APP_ROOT"

echo "[3/7] 部署代码（当前目录 → $APP_ROOT）…"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '.next' --exclude '.wheels' \
  --exclude 'backend/vendor' --exclude 'backend/.env' --exclude 'backend/snapshots' \
  "$REPO_DIR/" "$APP_ROOT/"

echo "[4/7] 后端：venv + 依赖 + 迁移…"
cd "$APP_ROOT/backend"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m alembic upgrade head

echo "[5/7] 前端：pnpm 安装 + 生产构建…"
cd "$APP_ROOT/web"
corepack enable
pnpm install --frozen-lockfile
NEXT_TELEMETRY_DISABLED=1 pnpm run build

echo "[6/7] 安装 systemd 服务与 nginx 配置…"
cp "$APP_ROOT/deploy/systemd/carsel-api.service" /etc/systemd/system/
cp "$APP_ROOT/deploy/systemd/carsel-web.service" /etc/systemd/system/
cp "$APP_ROOT/deploy/systemd/carsel-sales-fetch.service" /etc/systemd/system/
cp "$APP_ROOT/deploy/systemd/carsel-sales-fetch.timer" /etc/systemd/system/
sed -i "s|/usr/bin/python3|$APP_ROOT/backend/.venv/bin/python|" /etc/systemd/system/carsel-api.service
cp "$APP_ROOT/deploy/nginx.conf" /etc/nginx/conf.d/carsel.conf
echo "   → 请编辑 /etc/nginx/conf.d/carsel.conf 的 server_name 为已备案域名，然后执行："
echo "     nginx -t && systemctl reload nginx"
systemctl daemon-reload
systemctl enable --now carsel-api carsel-web carsel-sales-fetch.timer
# 部署后立即试跑一次销量获取（幂等：已有数据则跳过；未发布则明日自动重试）
systemctl start carsel-sales-fetch.service

echo "[7/7] 部署完成。检查："
echo "  systemctl status carsel-api carsel-web nginx"
echo "  curl -s http://127.0.0.1:8000/api/v1/health"
echo "  curl -sI http://127.0.0.1:3000/"
