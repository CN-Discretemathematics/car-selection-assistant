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
#   - **远端 SHA 必须两处交叉核对才可信**（2026-10-21 事故后加，见下）
set -uo pipefail

REPO="CN-Discretemathematics/car-selection-assistant"
BRANCH="main"
# 2026-10-05：路径改为**可用环境变量覆盖**（默认值与生产完全一致，行为不变）。
# 动机：这个脚本此前**无法被测试**——`/var/lib/carsel`、`/var/log/...` 写死，
# 本机跑 `--check` 会在 `mkdir /var` 处直接 Permission denied，于是
# 「三 SHA 比对」「不一致告警」这两段新逻辑只能靠人读代码判断对错。
# 现在可以在临时目录里打桩 curl 与状态文件，真跑一遍三个分支（见
# `.tools/verify_deploy_check.sh`，Git Bash 下实测通过）。
APP_DIR="${CARSEL_APP_DIR:-/srv/carsel}"
STATE_DIR="${CARSEL_STATE_DIR:-/var/lib/carsel}"
STATE_FILE="${STATE_DIR}/deployed-${BRANCH}.sha"
PREV_FILE="${STATE_DIR}/previous-${BRANCH}.sha"
LOCK_FILE="${STATE_DIR}/deploy.lock"
DEPLOY_LOG="${CARSEL_DEPLOY_LOG:-/var/log/carsel-deploy.log}"
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

# ── 取远端 SHA：三层防护，缺一不可（2026-10-21 事故后加）────────────────────
#
# 事故（真实发生，不是假想）：2026-10-08 ~ 10-21 连续 **13 天**，本脚本每天在
# 05:00 记一行「已是最新（621833da…），无需部署」，而 main 早已前进几十笔。
# 生产因此停在 13 天前——durable memory、拒绝反馈、账户合并、多轮评测**全部没上线**。
#
# 根因不是脚本逻辑写错，而是**把一次未认证的 API 读当成了唯一事实源**：
# 那次读到的��**陈旧响应**（疑似中间链路的 HTTP 缓存），脚本毫无察觉，
# 于是「远端 == 已部署」这个判断在陈旧读上恒为真 ⇒ 每天安静地跳过。
#
# 三层防护各自对应一个独立的失效模式（缺一层就还会再坏一次）：
#   ① cache-buster：URL 每次唯一 ⇒ 任何 HTTP 缓存都命不中（治本）
#   ② 两个独立端点交叉核对：/commits/{branch} 与 /branches/{branch} 的 SHA 必须一致
#   ③ 源码包时间戳 vs API 提交时间：codeload 源码包的 mtime 就是它那个提交的
#      提交时间（实测：main 的包 mtime = 该提交的 committer date）。
#      两者差出容差 ⇒ 必有一方陈旧 ⇒ 报错退出，**不猜**。
remote_json=$(curl -fsSL -m 30 -H 'Cache-Control: no-cache' \
  "https://api.github.com/repos/${REPO}/commits/${BRANCH}?_=$(date +%s)" 2>/dev/null)
remote_sha=$(printf '%s' "$remote_json" | sed -n 's/.*"sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
# committer 的 date 是第 2 个 "date"（第 1 个是 author 的；合并提交两者相同）
remote_date=$(printf '%s' "$remote_json" | grep -o '"date": *"[0-9-]*T[0-9:]*Z"' | sed -n 2p \
  | sed 's/.*"\([0-9-]*T[0-9:]*Z\)"/\1/')
[ -n "${remote_date:-}" ] || remote_date=$(printf '%s' "$remote_json" \
  | grep -o '"date": *"[0-9-]*T[0-9:]*Z"' | sed -n 1p | sed 's/.*"\([0-9-]*T[0-9:]*Z\)"/\1/')
branch_json=$(curl -fsSL -m 30 -H 'Cache-Control: no-cache' \
  "https://api.github.com/repos/${REPO}/branches/${BRANCH}?_=$(date +%s)" 2>/dev/null)
branch_sha=$(printf '%s' "$branch_json" | sed -n 's/.*"sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
if [ -z "${remote_sha:-}" ]; then
  log "ERROR 无法获取 ${BRANCH} 最新 SHA（网络或 API 限流），本次跳过"
  exit 1
fi
if [ -n "${branch_sha:-}" ] && [ "$branch_sha" != "$remote_sha" ]; then
  log "ERROR 两个 GitHub 端点给出的 ${BRANCH} SHA 不一致（commits=${remote_sha} branches=${branch_sha}）"
  log "      至少有一个返回了陈旧数据（疑似中间链路缓存）——本次跳过，不猜"
  exit 1
fi
current_sha=$(cat "$STATE_FILE" 2>/dev/null || echo "")

# 「已部署多久」= 状态文件的最后写入时间（部署成功时写）。
# 它是**第三个独立信号**：若它已经很旧，而 API 偏偏说「与已部署相同」，
# 那这个「相同」就不可信（正是 13 天事故的形状）⇒ 不走快路径，改用源码包核对。
state_mtime=$(stat -c %Y "$STATE_FILE" 2>/dev/null || echo 0)
[ "${state_mtime:-0}" -gt 0 ] 2>/dev/null || state_mtime=0
state_age_days=0
if [ "$state_mtime" -gt 0 ]; then
  state_age_days=$(( ( $(date +%s) - state_mtime ) / 86400 ))
fi
STALE_DAYS="${CARSEL_DEPLOY_STALE_DAYS:-5}"

if [ "$MODE" = "check" ]; then
  log "远端 ${BRANCH}=${remote_sha}"
  log "已部署=${current_sha:-<未知>}（${state_age_days} 天前装的）"
  # 运行中容器自报的提交号（需镜像构建时注入 GIT_SHA，见 /api/v1/version）
  running_sha=$(curl -fsSL -m 10 "${HEALTH_URL%/ready}/version" 2>/dev/null \
    | sed -n 's/.*"commit_sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
  log "运行中=${running_sha:-<未注入/未知>}"
  [ "$remote_sha" = "$current_sha" ] && log "状态：已是最新" || log "状态：有更新待部署"
  # 三者不一致时**明确指出**：状态文件说最新、但跑的容器不是那个提交，
  # 说明有人绕过脚本部署过（2026-10-05 #65 带着 CI 红灯被合并即属此类）。
  if [ -n "$running_sha" ] && [ "$running_sha" != "$remote_sha" ]; then
    log "⚠️ 运行中的容器与 main 不一致：站外验版本请 curl .../api/v1/version"
  fi
  if [ "$state_age_days" -gt "$STALE_DAYS" ]; then
    log "⚠️ 已 ${state_age_days} 天没有部署过（阈值 ${STALE_DAYS} 天）"
    log "   若期间 main 有过更新，说明取 SHA 的链路在返回陈旧数据（2026-10-21 事故的形状）"
  fi
  exit 0
fi

# 快路径：远端 == 已部署，**且** 状态文件不算太旧。
# 「状态文件太旧」这一条正是 13 天事故的形状：那时 API 每天返回同一个陈旧 SHA，
# 于是这条快路径天天命中。现在它不成立了——宁可多下一次源码包去核对。
if [ "$remote_sha" = "$current_sha" ] && [ "$FORCE" != "1" ] && [ "$state_age_days" -le "$STALE_DAYS" ]; then
  log "已是最新（${remote_sha}），无需部署"
  exit 0
fi
if [ "$remote_sha" = "$current_sha" ] && [ "$FORCE" != "1" ]; then
  log "WARN 已 ${state_age_days} 天没有部署过，却读到 ${BRANCH}=${remote_sha} 与已部署相同"
  log "     这个「相同」**不可信**：改用源码包的提交时间核对，确认为真才算「已是最新」"
fi

# 单实例锁（--check 不加锁；dry-run 也加，避免与真实部署并行下载）
#
# 2026-10-03 修两处：
# 1) 锁文件放在 /var/lock —— **非 root 用户没有写权限**，`exec 9>` 直接失败。
#    改用 $STATE_DIR（部署用户本就可写，脚本已经往里写 STATE_FILE）。
# 2) 更要紧的是失败**被误报**：脚本是 `set -uo pipefail`（**没有 -e**），所以
#    `exec 9>` 失败后继续往下走，`flock -n 9` 随之失败，于是走进
#    「已有部署在运行，本次跳过」并 `exit 0` —— **权限错误被当成「有人在部署」，
#    还以成功退出**。运维看到的是一句像模像样的提示，而部署根本没发生。
#    现在把「拿不到锁」与「锁被占用」分开：前者报错退出 1，不再伪装成跳过。
if ! exec 9>"$LOCK_FILE" 2>/dev/null; then
  log "ERROR 无法打开锁文件 ${LOCK_FILE}（权限不足？当前用户 $(id -un)）"
  log "      请改用 sudo 运行，或修好该目录权限；**不会**继续部署"
  exit 1
fi
if ! flock -n 9; then
  log "已有部署在运行（锁 ${LOCK_FILE} 被占用），本次跳过"
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

# 2) 交叉核对：源码包的提交时间 vs API 的提交时间
#
# codeload 生成的源码包里，每个条目的 mtime 就是**该提交的提交时间**
# （实测：main 的包 mtime = 该提交的 committer date，06:30 UTC 对得上）。
# 于是它是一个**与 API 独立**的新鲜度信号：API 说 10-08、源码包说 10-21，
# 必有一方陈旧 —— 这种时候**不猜**，报错退出。
#
# 为什么必须放到下载之后：只有拿到包，才能知道包有多新。
tarball_epoch=$(stat -c %Y "$src_dir" 2>/dev/null || echo 0)
remote_epoch=$(date -d "${remote_date:-}" +%s 2>/dev/null || echo 0)
[ "${tarball_epoch:-0}" -gt 0 ] 2>/dev/null || tarball_epoch=0
[ "${remote_epoch:-0}" -gt 0 ] 2>/dev/null || remote_epoch=0
if [ "$tarball_epoch" -gt 0 ] && [ "$remote_epoch" -gt 0 ]; then
  drift=$(( tarball_epoch - remote_epoch ))
  [ "$drift" -lt 0 ] && drift=$(( -drift ))
  if [ "$drift" -gt 21600 ]; then
    log "ERROR 源码包的提交时间与 API 的提交时间相差 ${drift}s（容差 6h）"
    log "      源码包=$(date -u -d "@$tarball_epoch" '+%F %T') UTC  API=$(date -u -d "@$remote_epoch" '+%F %T') UTC"
    log "      有一方是陈旧数据——本次跳过，不猜"
    exit 1
  fi
  log "提交时间核对一致：源码包=$(date -u -d "@$tarball_epoch" '+%F %T') UTC  API=$(date -u -d "@$remote_epoch" '+%F %T') UTC"
else
  log "WARN 提交时间核对做不了（源码包或 API 的时间戳取不到），本次只按 SHA 判断"
fi

# 3) 重新判定「已是最新」——这次是**核对过**的（走快路径到这里说明状态文件太旧）
if [ "$remote_sha" = "$current_sha" ] && [ "$FORCE" != "1" ]; then
  log "已是最新（${remote_sha}，已用源码包的提交时间核对）"
  exit 0
fi

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
# 2026-10-05：把本次部署的提交号与构建时间注入镜像，供 /api/v1/version 回报。
# 目的：让「服务器跑的 == main 最新」**从站外可验**（curl .../api/v1/version），
# 而不必 SSH 上来读 deployed-main.sha。此前站外唯一的信号是 /health 的 200，
# 而它返回写死的 app_version——「接口活着」被误当成「版本是最新的」。
GIT_SHA="${remote_sha}" BUILD_TIME="$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  docker compose build api web || {
  log "ERROR 镜像构建失败，保持当前版本运行（未重启容器）"
  exit 1
}
log "镜像构建完成（commit=${remote_sha}）"

# 关于数据库迁移（2026-10-03 查证，**本脚本不需要加这一步**）
#
# 我一度以为「迁移漏执行会让部署静默瘫掉」——那是**错的**，这里留一句以免后人重蹈：
# deploy/backend.Dockerfile 的 CMD 是
#   python -m alembic upgrade head && python -m uvicorn ...
# 即**迁移在容器启动时就跑**，且用 `&&` 串联：迁移失败 → uvicorn 根本不启动 →
# 容器起不来 → /ready 无响应 → 本脚本健康检查失败 → 自动回滚到上一版镜像。
# 所以「代码合并了、迁移忘执行」在本部署流程里是**抓得住**的。
#
# 2026-10-03 生产库停在 c8a1f2e4b9d3 而 main 已含 b7e4c1d90a25，只是因为
# **新镜像还没部署**（旧镜像里没有那个迁移文件，无从执行），不是流程缺陷。

# 5) 切流
#
# ── 共享目录 `backend/.tmp` 的权限 ──────────────────────────────────────
#
# compose 把宿主 `backend/.tmp` bind-mount 进 api 容器，管理审计日志
# （admin-audit.log）、dense 构建水位标记、销量导入 flag 都落在这里
# ⇒ 容器里那个身份必须能写。
#
# 身份有**两个**（2026-10-23 实测）：
#   ① 容器内 uid 10001（carsel，见 deploy/backend.Dockerfile）；
#   ② 宿主 root —— 夜间任务 `carsel-nightly.sh` 在宿主上跑，
#      以 root 读/删 `sales-changed.flag`、读水位标记。
#
# ⚠️ compose 里 `cap_drop: ALL` ⇒ 连**以 root 跑**的容器都没有 DAC_OVERRIDE
# ⇒ 权限位是唯一依据（uid 0 ≠ 无所不能）⇒ 两种身份都得能写：
#   目录 1777（sticky：谁都能写，但只有属主能删自己的文件）
#   + 现有文件 666（新建文件由 umask 决定，故每次部署都重新放开一遍）。
#
# 写不进去的后果**不是**接口报错（审计写入已吞 OSError），而是更隐蔽的两个：
#   ① 管理审计日志不再落盘（只剩 docker logs 里的那一份）；
#   ② **dense 构建水位标记写不进去** ⇒ 状态页永远判「向量集合落后」
#      ⇒ 夜间任务每晚都白全量重建一次（5 分钟 CPU + embedding 费用）。
#
# chown 放在 chmod 之后：chown 可能清掉 setuid/setgid 位，顺序反了权限位会被抹掉。
# 目录本身不会被 rsync 覆盖（RSYNC_ARGS 已排除 `backend/.tmp/`），
# 但每次部署都重新授权一次，防止有人手工改过属主/权限后被静默带进生产。
mkdir -p "$APP_DIR/backend/.tmp"
chmod 1777 "$APP_DIR/backend/.tmp" 2>/dev/null || true
find "$APP_DIR/backend/.tmp" -maxdepth 1 -type f -exec chmod 666 {} + 2>/dev/null || true
chown -R 10001:10001 "$APP_DIR/backend/.tmp" 2>/dev/null || true

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

# 6.5) 非 root 断言（2026-10-22）：容器必须**不是 root**，否则回滚。
#
# Dockerfile 里写 `USER carsel` 只是**声明**——真正要保证的是跑起来时确实是那个身份：
# compose 里任何一处 `user:` 覆盖、镜像构建漏了 USER 指令，声明都会失效。
# ⇒ 部署后当场断言，失败即回滚（与健康检查同级）。
if ! bash "$APP_DIR/deploy/tests/verify_non_root.sh" deploy-api-1 deploy-web-1; then
  log "ERROR 容器以 root 运行（见上），回滚到上一版镜像"
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
{"sha":"${remote_sha}","previous":"${prev_deployed}","at":"$(date -Is)","status":"ok","host":"$(hostname)","src_date":"$(date -u -d "@${tarball_epoch:-0}" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "")"}
EOF
log "部署完成：${remote_sha}（ready=${api_code} site=${site_code}）"
exit 0
