#!/bin/bash
# carsel-nightly.sh — 每日 02:30 定时任务：销量导入（幂等）→ 有新数据则重建向量索引
#
#   carsel-nightly.sh              正常流程：导入；仅当写入 sales-changed.flag（新月份到位）才重建
#   carsel-nightly.sh --force      无论有无新月份都重建一次（数据回补/手工修数后用）
#   carsel-nightly.sh --no-rebuild 只导入，不重建（排障用）
#
# 由 crontab 调用：30 2 * * * /usr/local/bin/carsel-nightly.sh
#
# 设计要点
#   - 稠密（Zilliz）：tools/build_retrieval_index.py --target dense，全量重灌并写入水位标记
#     （.tmp/dense-build-meta.json，含构建时库内规模，供 /admin/rag/status 判断是否落后）；
#   - 稀疏（BM25）：**必须走管理接口**在运行中的 api 进程内重建——另起进程跑
#     `--target sparse` 建出来的索引会随该进程退出而丢弃（生产模式不会按数据量自动重建）；
#   - 车系摘要切片文本含「YYYY-MM 月销量 N 辆」，因此销量数据变化后不重建 = RAG 回答里的
#     销量是旧的（2026-09 实测：数据补齐后水位仍报新鲜，已改为「月份 + 规模」双比对）。
set -uo pipefail

APP_DIR="/srv/carsel"
CONTAINER="deploy-api-1"
FLAG="${APP_DIR}/backend/.tmp/sales-changed.flag"
TOKEN_FILE="/root/carsel-admin-token.txt"
API="http://127.0.0.1:8000/api/v1"
LOG="${APP_DIR}/logs/sales-cron.log"
REBUILD_LOG="${APP_DIR}/logs/rebuild-cron.log"

MODE="auto"
case "${1:-}" in
  --force) MODE="force" ;;
  --no-rebuild) MODE="no-rebuild" ;;
  "") ;;
  *) echo "用法: $0 [--force|--no-rebuild]"; exit 2 ;;
esac

mkdir -p "$(dirname "$LOG")"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

# 日志自截断（保留最近 3000 行），避免长期运行撑爆磁盘
for f in "$LOG" "$REBUILD_LOG"; do
  if [ -f "$f" ] && [ "$(wc -l < "$f")" -gt 4000 ]; then
    tail -n 3000 "$f" > "${f}.tmp" && mv "${f}.tmp" "$f"
  fi
done

# 0) 自更新：仓库里的本脚本若变化，安装到 /usr/local/bin（下次运行生效）
if [ -f "${APP_DIR}/deploy/carsel-nightly.sh" ] && ! cmp -s "${APP_DIR}/deploy/carsel-nightly.sh" /usr/local/bin/carsel-nightly.sh; then
  install -m 755 "${APP_DIR}/deploy/carsel-nightly.sh" /usr/local/bin/carsel-nightly.sh \
    && log "夜间脚本已自更新（/usr/local/bin/carsel-nightly.sh，下次运行生效）"
fi

# 1) 销量导入（幂等：目标月已就绪时零操作）
log "=========== 夜间任务开始（mode=${MODE}）==========="
if ! docker exec "$CONTAINER" python tools/fetch_sales_scheduled.py >> "$LOG" 2>&1; then
  log "销量导入返回非 0（详见上方日志），继续判断是否需要重建"
fi

# 2) 是否需要重建
NEW_MONTH=""
if [ -f "$FLAG" ]; then
  NEW_MONTH=$(cat "$FLAG" 2>/dev/null)
  rm -f "$FLAG"
fi

if [ "$MODE" = "no-rebuild" ]; then
  log "按参数跳过重建（新月份标记=${NEW_MONTH:-无}）"
  exit 0
fi
if [ -z "$NEW_MONTH" ] && [ "$MODE" != "force" ]; then
  log "无新销量月份，跳过向量索引重建"
  exit 0
fi

log "开始重建向量索引（触发原因：${NEW_MONTH:+新月份 ${NEW_MONTH}}${NEW_MONTH:-手工 --force}）"

# 3) 稠密索引（Zilliz 全量重灌 + 水位标记）
START=$(date +%s)
if docker exec "$CONTAINER" python tools/build_retrieval_index.py --target dense >> "$REBUILD_LOG" 2>&1; then
  log "稠密重建完成（耗时 $(( $(date +%s) - START )) 秒）"
else
  log "稠密重建失败（详见 ${REBUILD_LOG}）——RAG 回答可能仍引用旧销量，请人工介入"
fi

# 4) 稀疏索引：必须打到运行中的 api 进程（管理接口），否则索引随子进程丢弃
if [ -r "$TOKEN_FILE" ]; then
  TOKEN=$(cat "$TOKEN_FILE")
  RESP=$(curl -s -m 600 -X POST -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
    -d '{"target":"sparse"}' "${API}/admin/rag/reindex" 2>&1)
  case "$RESP" in
    *'"target":"sparse"'*) log "稀疏（BM25）索引重建完成：${RESP:0:200}" ;;
    *) log "稀疏索引重建异常：${RESP:0:200}" ;;
  esac
else
  log "跳过稀疏重建：读取不到 ${TOKEN_FILE}"
fi

# 5) 重建后核对水位（月份 + 规模一致才算同步）
if [ -r "$TOKEN_FILE" ]; then
  curl -s -m 60 -H "Authorization: Bearer $(cat "$TOKEN_FILE")" "${API}/admin/rag/status" \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as err:
    print('  状态解析失败:', err); raise SystemExit(0)
dense, sparse = d.get('dense') or {}, d.get('sparse') or {}
print('  稠密: chunks=%s stale=%s reason=%s' % (dense.get('chunks'), dense.get('stale'), dense.get('stale_reason')))
print('  稀疏: chunks=%s' % sparse.get('chunks'))
" >> "$LOG" 2>&1
fi

log "=========== 夜间任务结束 ==========="
exit 0
