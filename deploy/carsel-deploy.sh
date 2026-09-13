#!/bin/bash
# carsel-deploy.sh — 只部署 origin/main 的自动部署脚本（服务器侧运行，无需 git）
#
#   carsel-deploy.sh            对比 GitHub 上 main 的最新提交，有更新则同步源码 → 重建镜像 → 切流 → 健康检查
#   carsel-deploy.sh --check    只检查（打印远端 SHA 与当前已部署 SHA，不下载不构建）
#   carsel-deploy.sh --dry-run  走完下载/解包/rsync 预览，但不构建、不重启容器
#   carsel-deploy.sh --force    忽略 SHA 相同，强制执行一次部署（手工重部署用）
#
# 设计要点：
#   - 只认 main：SHA 取自 GitHub API（api.github.com 在大陆节点可达；github.com 本身可能超时，不依赖它）
#   - 源码包取自 codeload（公开仓库，无需凭据；仓库内不含 .env，服务器 .env 由 rsync --exclude 保护）
#   - 先构建后切换：构建失败保持旧版本继续服务
#   - 切换后做健康检查（/api/v1/ready 就绪 + 首页 200），失败自动回滚到上一版镜像
#   - flock 单实例：不会与手工部署或上一次未完成的部署叠加
set -uo pipefail

REPO="CN-Discretemathematics/car-selection-assistant"
BRANCH="main"
APP_DIR="/srv/carsel"
STATE_DIR="/var/lib/carsel"
STATE_FILE="${STATE_DIR}/deployed-${BRANCH}.sha"
PREV_FILE="${STATE_DIR}/previous-${BRANCH}.sha"
LOCK_FILE="/var/lock/carsel-deploy.lock"
DEPLOY_LOG="/var/log/carsel-deploy.log"
HEALTH_URL="http://127.0.0.1:8000/api/v1/ready"
SITE_URL="http://127.0.0.1/"

MODE="deploy"
case "${1:-}" in
  --check) MODE="check" ;;
  --dry-run) MODE="dry-run" ;;
  --force) MODE="deploy"; FORCE=1 ;;
  "") ;;
  *) echo "用法: $0 [--check|--dry-run|--force]"; exit 2 ;;
esac
FORCE="${FORCE:-0}"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# 日志自截断（保留最近 2000 行），避免长期运行撑爆磁盘
if [ -f "$DEPLOY_LOG" ] && [ "$(wc -l < "$DEPLOY_LOG")" -gt 2500 ]; then
  tail -n 2000 "$DEPLOY_LOG" > "${DEPLOY_LOG}.tmp" && mv "${DEPLOY_LOG}.tmp" "$DEPLOY_LOG"
fi

mkdir -p "$STATE_DIR"

remote_sha=$(curl -fsSL -m 30 "https://api.github.com/repos/${REPO}/commits/${BRANCH}" 2>/dev/null \
  | sed -n 's/.*"sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
if [ -z "${remote_sha:-}" ]; then
  log "ERROR 无法获取 ${BRANCH} 最新 SHA（网络或 API 限流），本次跳过"
  exit 1
fi
current_sha=$(cat "$STATE_FILE" 2>/dev/null || echo "")

if [ "$MODE" = "check" ]; then
  log "远端 ${BRANCH}=${remote_sha}"
  log "已部署=${current_sha:-<未知>}"
  [ "$remote_sha" = "$current_sha" ] && log "状态：已是最新" || log "状态：有更新待部署"
  exit 0
fi

if [ "$remote_sha" = "$current_sha" ] && [ "$FORCE" != "1" ]; then
  log "已是最新（${remote_sha}），无需部署"
  exit 0
fi

# 单实例锁（--check 不加锁；dry-run 也加，避免与真实部署并行下载）
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "已有部署在运行，本次跳过"
  exit 0
fi

log "开始部署：${current_sha:-<未知>} -> ${remote_sha}$([ "$FORCE" = "1" ] && echo '（--force）')"

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

# 1) 下载源码包
if ! curl -fsSL -m 300 -o "$tmp_dir/src.tar.gz" \
  "https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}"; then
  log "ERROR 源码包下载失败，本次跳过"
  exit 1
fi
tar -xzf "$tmp_dir/src.tar.gz" -C "$tmp_dir" || { log "ERROR 解包失败"; exit 1; }
src_dir=$(find "$tmp_dir" -maxdepth 1 -mindepth 1 -type d | head -1)
if [ ! -d "$src_dir/backend/app" ] || [ ! -f "$src_dir/deploy/docker-compose.yml" ]; then
  log "ERROR 源码包结构异常（${src_dir}），本次跳过"
  exit 1
fi
log "源码包就绪：$(du -sh "$tmp_dir/src.tar.gz" | cut -f1)"

# 2) 同步源码（--delete 让删除的文件也同步；服务器本地文件与运行期数据一律排除）
#    排除项说明：
#      backend/.tmp    compose 挂载卷（embedding 向量缓存 / dense 水位标记），删了会触发昂贵重建
#      backend/.env*   生产凭据与历史备份，绝不自动删除
#      web/public/.gitkeep  仓库尚未跟踪该文件（frontend.Dockerfile 却 COPY 它），
#                      保护到补进仓库为止，否则构建会因缺目录失败
#      reviewer/REVIEWER_AGENT.md  有意 gitignore 的内部评审规范
RSYNC_ARGS=(-a --delete
  --exclude 'backend/.env' --exclude 'backend/.env.*' --exclude 'deploy/.env' --exclude '.env'
  --exclude 'backend/.tmp/' --exclude 'backend/eval/' --exclude 'backend/vendor/'
  --exclude 'backend/.venv/' --exclude 'backend/.embed_cache.json' --exclude 'backend/snapshots/'
  --exclude 'logs/' --exclude '*.tar.gz' --exclude '*.log'
  --exclude 'node_modules/' --exclude '.next/'
  --exclude 'web/public/.gitkeep' --exclude 'reviewer/REVIEWER_AGENT.md')
if [ "$MODE" = "dry-run" ]; then
  log "dry-run：以下为将会同步的差异（不写入、不构建）"
  rsync "${RSYNC_ARGS[@]}" --dry-run --itemize-changes "$src_dir"/ "$APP_DIR"/ | head -40
  log "dry-run 结束（未改动任何文件）"
  exit 0
fi

# 3) 备份当前镜像（回滚用）
docker tag deploy-api:latest deploy-api:rollback 2>/dev/null || true
docker tag deploy-web:latest deploy-web:rollback 2>/dev/null || true
prev_deployed=$(cat "$STATE_FILE" 2>/dev/null || echo "")

if ! rsync "${RSYNC_ARGS[@]}" "$src_dir"/ "$APP_DIR"/; then
  log "ERROR 源码同步失败，未改动运行中的服务"
  exit 1
fi
log "源码已同步到 ${APP_DIR}"

# 3.5) 自更新：仓库里的部署脚本若与本机安装的不一致，安装之（下次运行生效）
if [ -f "${APP_DIR}/deploy/carsel-deploy.sh" ] && ! cmp -s "${APP_DIR}/deploy/carsel-deploy.sh" /usr/local/bin/carsel-deploy.sh; then
  install -m 755 "${APP_DIR}/deploy/carsel-deploy.sh" /usr/local/bin/carsel-deploy.sh \
    && log "部署脚本已自更新（/usr/local/bin/carsel-deploy.sh，下次运行生效）"
fi

# 4) 构建（失败则保持当前版本继续服务）
cd "$APP_DIR/deploy" || { log "ERROR 缺少 ${APP_DIR}/deploy"; exit 1; }
if ! docker compose build api web; then
  log "ERROR 镜像构建失败，保持当前版本运行（未重启容器）"
  exit 1
fi
log "镜像构建完成"

# 5) 切流
if ! docker compose up -d; then
  log "ERROR 容器启动失败，尝试回滚"
  docker tag deploy-api:rollback deploy-api:latest 2>/dev/null
  docker tag deploy-web:rollback deploy-web:latest 2>/dev/null
  docker compose up -d
  exit 1
fi

# 6) 健康检查（API 就绪 + 站点可访问），失败自动回滚
healthy=0
for _ in $(seq 1 24); do
  sleep 5
  api_code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$HEALTH_URL" || true)
  site_code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$SITE_URL" || true)
  if [ "$api_code" = "200" ] && [ "$site_code" = "200" ]; then healthy=1; break; fi
done

if [ "$healthy" != "1" ]; then
  log "ERROR 健康检查失败（ready=${api_code:-无响应} site=${site_code:-无响应}），回滚到上一版镜像"
  docker tag deploy-api:rollback deploy-api:latest 2>/dev/null
  docker tag deploy-web:rollback deploy-web:latest 2>/dev/null
  docker compose up -d
  sleep 20
  log "已回滚；当前 ready=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$HEALTH_URL" || echo 无响应)"
  exit 1
fi

# 7) 记录成功状态
echo "$remote_sha" > "$STATE_FILE"
[ -n "$prev_deployed" ] && echo "$prev_deployed" > "$PREV_FILE"
cat > "${STATE_DIR}/last-deploy.json" <<EOF
{"sha":"${remote_sha}","previous":"${prev_deployed}","at":"$(date -Is)","status":"ok","host":"$(hostname)"}
EOF
log "部署完成：${remote_sha}（ready=${api_code} site=${site_code}）"
exit 0
