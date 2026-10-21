#!/bin/bash
# 验证 2026-10-21 加的「取 SHA 三层防护」真能挡住那些失效场景。
#
# 背景（真实事故）：2026-10-08 ~ 10-21 连续 13 天，本脚本每天记一行
# 「已是最新（621833da…），无需部署」，而 main 早已前进几十笔。
# 生产因此停在 13 天前。根因是**一次未认证的 API 读被当成了唯一事实源**。
#
# 本测试用桩 curl / docker / rsync 在临时目录里**真跑** carsel-deploy.sh，
# 不调真 GitHub、不碰真服务器、不写真 /var。
#
# 覆盖场景：
#   1 正常有更新          → 部署，状态文件写成新 SHA
#   2 正常「已是最新」    → 快路径退出，**不构建**
#   3 API 陈旧（事故形状）→ 报错退出 1，**不谎报已是最新**、不部署
#   4 源码包陈旧          → 报错退出 1，不部署陈旧代码
#   5 两个端点不一致      → 报错退出 1
#   6 两边都陈旧          → 核对通过（这一路**检测不出来**，靠 cache-buster 治本）
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# 可用 CARSEL_DEPLOY_SCRIPT 指向别的脚本版本 ⇒ 同一套桩直接做 A/B：
# 把旧脚本喂进来，事故场景（场景 3）必须**红**——否则这条门禁测不出差别。
SCRIPT="${CARSEL_DEPLOY_SCRIPT:-$ROOT/deploy/carsel-deploy.sh}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "被测脚本: $SCRIPT"

export CARSEL_STATE_DIR="$TMP/state"
export CARSEL_DEPLOY_LOG="$TMP/deploy.log"
export CARSEL_APP_DIR="$TMP/app"
export CARSEL_DEPLOY_STALE_DAYS=5
mkdir -p "$CARSEL_STATE_DIR" "$CARSEL_APP_DIR/deploy" "$CARSEL_APP_DIR/backend/app"

A40=$(printf 'a%.0s' $(seq 40))
B40=$(printf 'b%.0s' $(seq 40))
C40=$(printf 'c%.0s' $(seq 40))
NEW_DATE="2026-10-21T06:30:42Z"
OLD_DATE="2026-10-08T05:03:41Z"

mkdir -p "$TMP/bin"
cat > "$TMP/bin/curl" <<'STUB'
#!/bin/bash
# 打桩 curl：按 URL 分支，不发任何真实请求
out=""; url=""; prev=""
for a in "$@"; do
  case "$prev" in -o) out="$a" ;; esac
  case "$a" in http*) url="$a" ;; esac
  prev="$a"
done
case "$url" in
  *api.github.com*/commits/*)
    printf '{"sha":"%s","commit":{"author":{"date":"%s"},"committer":{"date":"%s"}}}\n' \
      "$FAKE_API_SHA" "$FAKE_API_DATE" "$FAKE_API_DATE" ;;
  *api.github.com*/branches/*)
    printf '{"name":"main","commit":{"sha":"%s"}}\n' "$FAKE_BRANCH_SHA" ;;
  *codeload.github.com*)
    if [ -n "$out" ]; then
      work=$(mktemp -d)
      mkdir -p "$work/car-selection-assistant-main/backend/app" "$work/car-selection-assistant-main/deploy"
      echo "x" > "$work/car-selection-assistant-main/backend/app/__init__.py"
      echo "x" > "$work/car-selection-assistant-main/deploy/docker-compose.yml"
      ( cd "$work" && tar --mtime="$FAKE_TARBALL_DATE" -czf "$out" car-selection-assistant-main ) || exit 1
      rm -rf "$work"
    fi ;;
  */api/v1/version)
    printf '{"app_version":"0.1.0","commit_sha":"%s","build_time":"x"}\n' "$FAKE_RUNNING_SHA" ;;
  *)
    echo "200" ;;
esac
exit 0
STUB

cat > "$TMP/bin/docker" <<'STUB'
#!/bin/bash
# 打桩 docker：记录调用，便于断言「有没有真的去构建/切流」
echo "docker $*" >> "$DOCKER_TRACE"
exit 0
STUB

cat > "$TMP/bin/rsync" <<'STUB'
#!/bin/bash
echo "rsync $*" >> "$RSYNC_TRACE"
exit 0
STUB

cat > "$TMP/bin/flock" <<'STUB'
#!/bin/bash
# 打桩 flock：Git Bash 不带 util-linux 的 flock，缺了会被脚本当成「锁被占用」而跳过部署。
# 这里一律当作「拿到锁」——本测试要验的是取 SHA 的判定，不是互斥。
exit 0
STUB

chmod +x "$TMP/bin/curl" "$TMP/bin/docker" "$TMP/bin/rsync" "$TMP/bin/flock"
export PATH="$TMP/bin:$PATH"
export DOCKER_TRACE="$TMP/docker.trace"
export RSYNC_TRACE="$TMP/rsync.trace"

fail=0

# run_case <描述> <期望> <断言函数...>
# 环境变量：FAKE_API_SHA / FAKE_API_DATE / FAKE_BRANCH_SHA / FAKE_TARBALL_DATE /
#           FAKE_RUNNING_SHA / 当前状态文件内容 / 状态文件 mtime
run_case() {
  local desc="$1"; shift
  : > "$DOCKER_TRACE"; : > "$RSYNC_TRACE"
  [ -n "${STATE_SHA:-}" ] && printf '%s' "$STATE_SHA" > "$CARSEL_STATE_DIR/deployed-main.sha" \
    || rm -f "$CARSEL_STATE_DIR/deployed-main.sha"
  if [ -n "${STATE_AGE_DAYS:-0}" ] && [ "$STATE_AGE_DAYS" -gt 0 ]; then
    touch -d "@$(( $(date +%s) - STATE_AGE_DAYS * 86400 ))" "$CARSEL_STATE_DIR/deployed-main.sha"
  fi
  local out rc
  # 每个场景都加超时：某个场景卡住时，测试要**失败并指出是哪个**，而不是整体挂住
  out=$(timeout 60 bash "$SCRIPT" 2>&1); rc=$?
  echo "--- $desc"
  echo "$out" | sed 's/^/      /'
  echo "      [退出码 $rc]"
  local ok=1
  while [ "$#" -gt 0 ]; do
    # ⚠️ 不要写 `shift 2`：最后一个断言可能只有一个参数（build / nobuild），
    # 而 bash 的 `shift 2` 在参数不足时会**报错且不移动** ⇒ 死循环（实测卡死过）。
    local kind="$1"; shift
    local want="${1:-}"
    [ "$#" -gt 0 ] && shift
    case "$kind" in
      contains)  echo "$out" | grep -q "$want" || { echo "      ✘ 期望包含「$want」"; ok=0; } ;;
      notcontains) echo "$out" | grep -q "$want" && { echo "      ✘ 不该包含「$want」"; ok=0; } ;;
      rc)        [ "$rc" = "$want" ] || { echo "      ✘ 期望退出码 $want，实际 $rc"; ok=0; } ;;
      state)     local got; got=$(cat "$CARSEL_STATE_DIR/deployed-main.sha" 2>/dev/null || echo "<无>")
                 [ "$got" = "$want" ] || { echo "      ✘ 期望状态文件=$want，实际=$got"; ok=0; } ;;
      nobuild)   grep -q "compose build" "$DOCKER_TRACE" && { echo "      ✘ 不该发生构建"; ok=0; } ;;
      build)     grep -q "compose build" "$DOCKER_TRACE" || { echo "      ✘ 应该发生构建"; ok=0; } ;;
    esac
  done
  [ "$ok" = "1" ] && echo "      ✔ 全部断言通过" || fail=1
  echo
}

echo "=== 场景 1 正常有更新 → 应部署并把状态文件写成新 SHA ==="
STATE_SHA="$A40" STATE_AGE_DAYS=0 \
FAKE_API_SHA="$B40" FAKE_API_DATE="$NEW_DATE" FAKE_BRANCH_SHA="$B40" \
FAKE_TARBALL_DATE="$NEW_DATE" FAKE_RUNNING_SHA="$A40" \
run_case "远端已前进" contains "部署完成" rc 0 state "$B40" build

echo "=== 场景 2 正常「已是最新」→ 快路径退出，不构建 ==="
STATE_SHA="$A40" STATE_AGE_DAYS=0 \
FAKE_API_SHA="$A40" FAKE_API_DATE="$NEW_DATE" FAKE_BRANCH_SHA="$A40" \
FAKE_TARBALL_DATE="$NEW_DATE" FAKE_RUNNING_SHA="$A40" \
run_case "无更新" contains "已是最新" rc 0 nobuild

echo "=== 场景 3 API 陈旧（13 天事故的形状）→ 报错退出，不得谎报已是最新 ==="
STATE_SHA="$A40" STATE_AGE_DAYS=13 \
FAKE_API_SHA="$A40" FAKE_API_DATE="$OLD_DATE" FAKE_BRANCH_SHA="$A40" \
FAKE_TARBALL_DATE="$NEW_DATE" FAKE_RUNNING_SHA="$A40" \
run_case "API 读到陈旧 SHA" contains "源码包的提交时间与 API 的提交时间相差" \
  notcontains "已是最新（" rc 1 nobuild state "$A40"

echo "=== 场景 4 源码包陈旧 → 报错退出，不部署陈旧代码 ==="
STATE_SHA="$A40" STATE_AGE_DAYS=13 \
FAKE_API_SHA="$B40" FAKE_API_DATE="$NEW_DATE" FAKE_BRANCH_SHA="$B40" \
FAKE_TARBALL_DATE="$OLD_DATE" FAKE_RUNNING_SHA="$A40" \
run_case "源码包比 API 旧" contains "源码包的提交时间与 API 的提交时间相差" rc 1 nobuild state "$A40"

echo "=== 场景 5 两个 GitHub 端点不一致 → 报错退出 ==="
STATE_SHA="$A40" STATE_AGE_DAYS=0 \
FAKE_API_SHA="$B40" FAKE_API_DATE="$NEW_DATE" FAKE_BRANCH_SHA="$C40" \
FAKE_TARBALL_DATE="$NEW_DATE" FAKE_RUNNING_SHA="$A40" \
run_case "端点打架" contains "两个 GitHub 端点给出的" rc 1 nobuild state "$A40"

echo "=== 场景 6 两边都陈旧 → 核对通过（这一路检测不出，靠 cache-buster 治本）==="
STATE_SHA="$A40" STATE_AGE_DAYS=13 \
FAKE_API_SHA="$A40" FAKE_API_DATE="$OLD_DATE" FAKE_BRANCH_SHA="$A40" \
FAKE_TARBALL_DATE="$OLD_DATE" FAKE_RUNNING_SHA="$A40" \
run_case "两边都旧" contains "已是最新" rc 0 nobuild

echo "=== 场景 7 停滞告警：状态文件很旧 + 快路径本会命中 → 必须走核对 ==="
STATE_SHA="$A40" STATE_AGE_DAYS=13 \
FAKE_API_SHA="$A40" FAKE_API_DATE="$NEW_DATE" FAKE_BRANCH_SHA="$A40" \
FAKE_TARBALL_DATE="$NEW_DATE" FAKE_RUNNING_SHA="$A40" \
run_case "旧状态文件" contains "已用源码包的提交时间核对" rc 0 nobuild

echo
if [ "$fail" -eq 0 ]; then echo "全部用例通过"; else echo "有用例未通过"; fi
exit "$fail"
