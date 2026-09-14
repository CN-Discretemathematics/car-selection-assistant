#!/bin/bash
# carsel-sku-backfill.sh — 回补「有销量但没款型」的车系（SKU + 参数配置）
#
#   carsel-sku-backfill.sh [--check] [--save-raw] [--batch N] [--max-batches N]
#
# 背景（2026-09-14 用户实测「风云A9 销量正常但没有款型」）：
# 销量榜导入会给榜上车系创建**只有车系级信息**的存根（品牌/定位/能源/车身），SKU 要靠
# `tools/fetch_autohome_sku.py` 单独抓；此前只补过车系资料 → 201 个在售车系没有款型
# （详情页「暂无在售款型数据」、无配置表），而汽车之家上这些车系的 SKU 都能抓到。
#
# 设计要点（依据 2026-09-14 独立审查报告 M1/M2/M3/m6-m9 修订）
#   * **以数据库为唯一判据**：每批的 id 现查现取（`tools/export_series_gaps.py --ids`），
#     不依赖静态清单与"已完成"文件——补好的车系自然从缺口里消失，天然幂等可续跑；
#   * **每批校验**：抓完再查一次缺口数，没有下降就**停下并报错**，绝不把失败记成完成
#     （`fetch_autohome_sku.py` 的各阶段恒返回 0，只看退出码会静默漏数据）；
#   * **显式 `--stage sku`**：默认 `all` 会每批重抓 26 个 A-Z 索引页（浪费且有额外失败面）；
#     首轮如需刷新索引，用 `--refresh-index` 先单独跑一次；
#   * BATCH 必须为正整数（`BATCH=0` 会造成死循环 + 全库重抓，属可造成外部影响的手误）。
#
# 运行位置：服务器（需访问汽车之家 + RDS）
#   安装：install -m 755 /srv/carsel/deploy/carsel-sku-backfill.sh /usr/local/bin/carsel-sku-backfill.sh
#   运行：nohup /usr/local/bin/carsel-sku-backfill.sh > /tmp/backfill-nohup.log 2>&1 &
#   日志：/srv/carsel/logs/sku-backfill.log
#
# 回补完成后**必须重建向量索引**（款型/参数变化 → 切片内容变化）：
#   docker exec -w /srv/carsel/backend deploy-api-1 python tools/build_retrieval_index.py --target dense
#   curl -X POST -H "Authorization: Bearer $(cat /root/carsel-nightly-token.txt)" \
#     -H 'Content-Type: application/json' -d '{"target":"sparse"}' localhost:8000/api/v1/admin/rag/reindex
set -uo pipefail

BATCH=${BATCH:-25}
LOG=${LOG:-/srv/carsel/logs/sku-backfill.log}
CONTAINER=${CONTAINER:-deploy-api-1}
WORKDIR=/srv/carsel/backend
SAVE_RAW=0
MAX_BATCHES=0        # 0 = 直到缺口清空
REFRESH_INDEX=0
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --save-raw) SAVE_RAW=1 ;;
    --refresh-index) REFRESH_INDEX=1 ;;
    --batch) BATCH=${2:-}; shift ;;
    --max-batches) MAX_BATCHES=${2:-}; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

case "$BATCH" in
  ''|*[!0-9]*) echo "BATCH 必须是正整数（当前 '$BATCH'）" >&2; exit 2 ;;
esac
[ "$BATCH" -ge 1 ] || { echo "BATCH 必须 >= 1" >&2; exit 2; }
case "$MAX_BATCHES" in
  ''|*[!0-9]*) echo "MAX_BATCHES 必须是非负整数" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$LOG")" || { echo "无法创建日志目录 $(dirname "$LOG")" >&2; exit 1; }
: >> "$LOG" 2>/dev/null || { echo "日志不可写：$LOG" >&2; exit 1; }

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
in_container() { docker exec -w "$WORKDIR" "$CONTAINER" "$@"; }

gap_report() { in_container python tools/export_series_gaps.py --report 2>/dev/null; }
# 缺口计数也在容器内解析（宿主机未必有 python3；此前用宿主 python3 会因其缺失而每批中止）
gap_field() {
  gap_report | in_container python -c \
    "import json,sys; print(json.load(sys.stdin).get('$1',''))" 2>/dev/null
}
# 回补目标 = 完全无款型的「真缺口」（有款型但全停售属数据完整，补不了也不需要补）
gap_count() { gap_field gap_no_variants; }

log "=========== 款型回补启动（batch=$BATCH, save_raw=$SAVE_RAW, max_batches=$MAX_BATCHES）==========="
START_GAP=$(gap_count)
if [ -z "$START_GAP" ]; then
  log "错误：无法读取缺口报告（容器 $CONTAINER 或数据库不可达），未开始抓取"
  echo "无法读取缺口报告（容器或数据库不可达）" >&2
  exit 1
fi
log "起始缺口：$START_GAP"

if [ "$CHECK_ONLY" = "1" ]; then
  report=$(gap_report)
  if [ -z "$report" ]; then
    echo "无法读取缺口报告（容器或数据库不可达）" >&2
    exit 1
  fi
  printf '%s\n' "$report"
  exit 0
fi

exec 9>/tmp/sku-backfill.lock || exit 1
flock -n 9 || { echo "另一个回补进程在跑，退出"; exit 1; }

if [ "$REFRESH_INDEX" = "1" ]; then
  log "按参数先刷新汽车之家车系索引（--stage index）"
  in_container python tools/fetch_autohome_sku.py --stage index >> "$LOG" 2>&1
fi

batch_no=0
while :; do
  # 区分「工具失败」与「确实没有可回补 id」：失败必须报错退出，不能当成完成
  # （第二轮审查 M2a：此前 docker exec 失败 → stdout 空 → 误报「缺口已清空」并退出 0）
  IDS_OUT=$(in_container python tools/export_series_gaps.py --ids --limit "$BATCH" --quiet 2>>"$LOG")
  IDS_RC=$?
  if [ "$IDS_RC" -ne 0 ]; then
    log "错误：导出缺口 id 失败（退出码 $IDS_RC），停止执行"
    echo "导出缺口 id 失败（退出码 $IDS_RC）" >&2
    exit 1
  fi
  BATCH_IDS=$(printf '%s' "$IDS_OUT" | tr -d '\r\n')
  BEFORE=$(gap_count)
  if [ -z "$BEFORE" ]; then
    log "错误：无法读取缺口数，停止执行"
    exit 1
  fi
  if [ -z "$BATCH_IDS" ]; then
    # 没有可回补的 id：只有缺口真的归零才算成功（M2b：剩余缺口若都无汽车之家映射，
    # 超出回补范围，但必须如实报出，不能宣称完成）
    if [ "$BEFORE" -eq 0 ]; then
      log "缺口已归零，回补完成"
      break
    fi
    MISSING=$(gap_field gap_without_autohome_ref)
    log "停止：真缺口（完全无款型）已无可回补的汽车之家 id（无映射 $MISSING 个）"
    echo "真缺口无可回补 id（无汽车之家映射 $MISSING 个），需人工处理" >&2
    exit 1
  fi
  COUNT=$(awk -F, '{print NF}' <<< "$BATCH_IDS")
  batch_no=$((batch_no + 1))
  log "---- 第 $batch_no 批：$COUNT 个车系，处理前缺口 $BEFORE ----"

  RAW_FLAG=""
  [ "$SAVE_RAW" = "1" ] && RAW_FLAG="--save-raw"
  # 显式 --stage sku：默认 all 会每批重抓 A-Z 索引页（审查 M1）
  in_container python tools/fetch_autohome_sku.py --stage sku --ids "$BATCH_IDS" $RAW_FLAG >> "$LOG" 2>&1
  RC=$?

  AFTER=$(gap_count)
  log "第 $batch_no 批结束：退出码 $RC，处理后缺口 $AFTER（本批 $COUNT 个）"

  if [ -z "$AFTER" ]; then
    log "无法读取缺口数（数据库/容器异常），停止以免误判完成"
    exit 1
  fi
  if [ "$AFTER" -ge "$BEFORE" ]; then
    log "本批缺口未下降（$BEFORE → $AFTER）：说明这批没抓成功或该车系确实无 SKU。"
    log "停止执行，请人工查看上方抓取输出（退出码 $RC）后重跑；不做无意义循环。"
    exit 1
  fi
  log "本批缺口下降 $((BEFORE - AFTER)) 个"

  if [ "$MAX_BATCHES" != "0" ] && [ "$batch_no" -ge "$MAX_BATCHES" ]; then
    log "达到 --max-batches $MAX_BATCHES，主动停止"
    break
  fi
done

FINAL_GAP=$(gap_count)
log "=========== 款型回补结束：$batch_no 批，缺口 $START_GAP → ${FINAL_GAP:-未知} ==========="
log "请按脚本头部说明重建向量索引（稠密 + 稀疏）"
if [ -n "$FINAL_GAP" ] && [ "$FINAL_GAP" -gt 0 ]; then
  log "注意：仍有 $FINAL_GAP 个车系无在售款型（多为无汽车之家映射或上游无数据），未达 0"
fi
