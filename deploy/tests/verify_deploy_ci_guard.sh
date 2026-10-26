#!/bin/bash
# 验证 carsel-deploy.sh 的「部署前 CI 门禁」（2026-10-27 新增）。
#
# 用桩 curl / docker / rsync / flock 在临时目录里**真跑**脚本：
# 不调真 GitHub、不碰真服务器、不写真 /var。
#
# 覆盖场景：
#   1 CI 全绿（4 个必需 job success）→ 正常部署
#   2 CI 明确 failure            → 拒绝部署、写 last-deploy.json status=blocked
#   3 CI 一直 pending            → 等待超时后拒绝
#   4 pending 后转 success        → 等待后放行
#   5 GitHub API 查询失败        → 拒绝（不猜）
#   6 必需 job 缺失（gates）      → 等到超时后拒绝
#   7 DEPLOY_SKIP_CI_GUARD=1     → 手工放行，跳过门禁
#   8 --check                    → 只打印 CI 状态，不拦截、不部署
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/deploy/carsel-deploy.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "被测脚本: $SCRIPT"

export CARSEL_STATE_DIR="$TMP/state"
export CARSEL_DEPLOY_LOG="$TMP/deploy.log"
export CARSEL_APP_DIR="$TMP/app"
export CARSEL_DEPLOY_STALE_DAYS=5
export CARSEL_CI_GUARD_WAIT_SECONDS=2
export CARSEL_CI_GUARD_POLL_SECONDS=1
export CARSEL_CI_API_RETRIES=1
export CARSEL_CI_API_RETRY_SLEEP=0
export DEPLOY_SKIP_CI_GUARD=0
export CI_CALLS="$TMP/ci.calls"

mkdir -p "$CARSEL_STATE_DIR" "$CARSEL_APP_DIR/deploy/tests" "$CARSEL_APP_DIR/backend/app"
echo "x" > "$CARSEL_APP_DIR/deploy/docker-compose.yml"
printf '#!/bin/bash\nexit 0\n' > "$CARSEL_APP_DIR/deploy/tests/verify_non_root.sh"

A40=$(printf 'a%.0s' $(seq 40))
B40=$(printf 'b%.0s' $(seq 40))
NEW_DATE="2026-10-27T01:30:00Z"
mkdir -p "$TMP/bin"

cat > "$TMP/bin/curl" <<'STUB'
#!/bin/bash
out=""; url=""; prev=""
for a in "$@"; do
  case "$prev" in -o) out="$a" ;; esac
  case "$a" in http*) url="$a" ;; esac
  prev="$a"
done
# check-runs 的 URL 里也含 /commits/，必须先匹配
case "$url" in
  *check-runs*)
    if [ "${FAKE_CI_API_FAIL:-0}" = "1" ]; then exit 22; fi
    if [ -n "${FAKE_CI_SEQ:-}" ]; then
      n=$(cat "$CI_CALLS" 2>/dev/null || echo 0)
      n=$((n + 1))
      echo "$n" > "$CI_CALLS"
      f="$FAKE_CI_FILE"
      if [ "$n" -eq 1 ] && [ -n "${FAKE_CI_FILE_FIRST:-}" ]; then f="$FAKE_CI_FILE_FIRST"; fi
      cat "$f"
    else
      cat "$FAKE_CI_FILE"
    fi ;;
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
  *) echo "200" ;;
esac
exit 0
STUB

cat > "$TMP/bin/docker" <<'STUB'
#!/bin/bash
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
exit 0
STUB

chmod +x "$TMP/bin/curl" "$TMP/bin/docker" "$TMP/bin/rsync" "$TMP/bin/flock"
export PATH="$TMP/bin:$PATH"
export DOCKER_TRACE="$TMP/docker.trace"
export RSYNC_TRACE="$TMP/rsync.trace"

# 生成四种 check-runs 响应（字段顺序与真实 GitHub 紧凑 JSON 一致：
# name 之后隔几个字符串字段才到 status，status 之后紧接 conclusion）。
json_concl() {
  case "$1" in
    null) printf 'null' ;;
    *) printf '"%s"' "$1" ;;
  esac
}

write_ci_json() {
  local mode="$1" file="$2"
  local b="completed" bcon="success" g="completed" gcon="success"
  local w="completed" wcon="success" m="completed" mcon="success"
  if [ "$mode" = "failure" ]; then bcon="failure"; fi
  if [ "$mode" = "pending" ]; then b="in_progress"; bcon="null"; g="in_progress"; gcon="null"; w="in_progress"; wcon="null"; m="in_progress"; mcon="null"; fi
  local gates_obj=""
  if [ "$mode" != "missing" ]; then
    gates_obj=$(printf '{"id":2,"name":"gates (secrets + ui-copy + doc-sync)","node_id":"x","head_sha":"%s","status":"%s","conclusion":%s}' "$A40" "$g" "$(json_concl "$gcon")")
  fi
  cat > "$file" <<JSON
{"total_count":4,"check_runs":[
{"id":1,"name":"backend (pytest)","node_id":"x","head_sha":"$A40","status":"$b","conclusion":$(json_concl "$bcon")},
$gates_obj
{"id":3,"name":"web (lint + tsc + build)","node_id":"x","head_sha":"$A40","status":"$w","conclusion":$(json_concl "$wcon")},
{"id":4,"name":"migrations + 功能校验（postgres）","node_id":"x","head_sha":"$A40","status":"$m","conclusion":$(json_concl "$mcon")}
]}
JSON
}

write_ci_json success "$TMP/ci-success.json"
write_ci_json failure "$TMP/ci-failure.json"
write_ci_json pending "$TMP/ci-pending.json"
write_ci_json missing "$TMP/ci-missing.json"

fail=0

# run_case <描述> <断言...>
run_case() {
  local desc="$1"; shift
  : > "$DOCKER_TRACE"; : > "$RSYNC_TRACE"; : > "$CI_CALLS"
  rm -f "$CARSEL_STATE_DIR/last-deploy.json"
  if [ -n "${STATE_SHA:-}" ]; then
    printf '%s' "$STATE_SHA" > "$CARSEL_STATE_DIR/deployed-main.sha"
    touch -d "@$(( $(date +%s) - ${STATE_AGE_DAYS:-0} * 86400 ))" "$CARSEL_STATE_DIR/deployed-main.sha"
  else
    rm -f "$CARSEL_STATE_DIR/deployed-main.sha"
  fi
  local out rc
  out=$(timeout 40 bash "$SCRIPT" 2>&1); rc=$?
  echo "--- $desc"
  echo "$out" | sed 's/^/      /'
  echo "      [退出码 $rc]"
  local ok=1
  while [ "$#" -gt 0 ]; do
    local kind="$1"; shift
    local want="${1:-}"
    [ "$#" -gt 0 ] && shift
    case "$kind" in
      contains) echo "$out" | grep -q "$want" || { echo "      ✘ 期望包含「$want」"; ok=0; } ;;
      notcontains) echo "$out" | grep -q "$want" && { echo "      ✘ 不该包含「$want」"; ok=0; } ;;
      rc) [ "$rc" = "$want" ] || { echo "      ✘ 期望退出码 $want，实际 $rc"; ok=0; } ;;
      state) local got; got=$(cat "$CARSEL_STATE_DIR/deployed-main.sha" 2>/dev/null || echo "<无>")
             [ "$got" = "$want" ] || { echo "      ✘ 期望状态文件=$want，实际=$got"; ok=0; } ;;
      blocked) local json; json="$CARSEL_STATE_DIR/last-deploy.json"
               [ -f "$json" ] || { echo "      ✘ 缺少 last-deploy.json"; ok=0; }
               grep -q '"status":"blocked"' "$json" 2>/dev/null || { echo "      ✘ last-deploy.json 不是 blocked"; ok=0; }
               grep -q "\"reason\":\"$want\"" "$json" 2>/dev/null || { echo "      ✘ reason 不是 $want：$(cat "$json" 2>/dev/null)"; ok=0; } ;;
      noblocked) [ -f "$CARSEL_STATE_DIR/last-deploy.json" ] && { echo "      ✘ 不该写 blocked 状态"; ok=0; } ;;
      build) grep -q "compose build" "$DOCKER_TRACE" || { echo "      ✘ 应该发生构建"; ok=0; } ;;
      nobuild) grep -q "compose build" "$DOCKER_TRACE" && { echo "      ✘ 不该发生构建"; ok=0; } ;;
    esac
  done
  [ "$ok" = "1" ] && echo "      ✔ 全部断言通过" || fail=1
  echo
}

# 场景 1：CI 全绿 → 正常部署
export STATE_SHA="$A40" STATE_AGE_DAYS=0
export FAKE_API_SHA="$B40" FAKE_API_DATE="$NEW_DATE" FAKE_BRANCH_SHA="$B40"
export FAKE_TARBALL_DATE="$NEW_DATE" FAKE_RUNNING_SHA="$A40"
export FAKE_CI_FILE="$TMP/ci-success.json"
export FAKE_CI_API_FAIL=0
unset FAKE_CI_SEQ FAKE_CI_FILE_FIRST
export DEPLOY_SKIP_CI_GUARD=0
run_case "CI 全绿" contains "CI 门禁通过" contains "部署完成" rc 0 state "$B40" build noblocked

# 场景 2：CI 明确 failure → 拒绝，状态文件不变
export FAKE_CI_FILE="$TMP/ci-failure.json"
run_case "CI failure" contains "CI 门禁未通过" rc 1 blocked ci_failed state "$A40" nobuild

# 场景 3：一直 pending → 等待超时
export FAKE_CI_FILE="$TMP/ci-pending.json"
run_case "CI pending" contains "CI 门禁等待超时" rc 1 blocked ci_timeout state "$A40" nobuild

# 场景 4：第一次 pending，第二次 success → 等待后放行
export FAKE_CI_FILE="$TMP/ci-success.json" FAKE_CI_FILE_FIRST="$TMP/ci-pending.json" FAKE_CI_SEQ=1
run_case "pending 后转 success" contains "CI 门禁通过" contains "部署完成" rc 0 state "$B40" build noblocked
unset FAKE_CI_SEQ FAKE_CI_FILE_FIRST

# 场景 5：GitHub API 查询失败 → 拒绝
export FAKE_CI_FILE="$TMP/ci-success.json" FAKE_CI_API_FAIL=1
run_case "CI API 失败" contains "CI 状态查询失败" rc 1 blocked ci_api_unavailable state "$A40" nobuild
export FAKE_CI_API_FAIL=0

# 场景 6：必需 job gates 缺失 → 等到超时后拒绝
export FAKE_CI_FILE="$TMP/ci-missing.json"
run_case "必需 job 缺失" contains "CI 门禁等待超时" rc 1 blocked ci_timeout state "$A40" nobuild

# 场景 7：手工逃生阀 → 跳过门禁继续部署
export FAKE_CI_FILE="$TMP/ci-failure.json" DEPLOY_SKIP_CI_GUARD=1
run_case "DEPLOY_SKIP_CI_GUARD=1" contains "跳过部署前 CI 门禁" contains "部署完成" rc 0 state "$B40" build noblocked
export DEPLOY_SKIP_CI_GUARD=0

# 场景 8：--check 只打印 CI 状态，不拦截
export FAKE_CI_FILE="$TMP/ci-failure.json"
export STATE_SHA="$A40" STATE_AGE_DAYS=0
: > "$DOCKER_TRACE"; : > "$RSYNC_TRACE"; : > "$CI_CALLS"
rm -f "$CARSEL_STATE_DIR/last-deploy.json"
out=$(timeout 40 bash "$SCRIPT" --check 2>&1); rc=$?
echo "--- --check 遇到 CI 红灯"
echo "$out" | sed 's/^/      /'
echo "      [退出码 $rc]"
if [ "$rc" != "0" ] || ! echo "$out" | grep -q "CI ❌ backend (pytest)"; then
  echo "      ✘ --check 应打印 CI 红灯且仍 exit 0"; fail=1
else
  echo "      ✔ 全部断言通过"
fi
[ -f "$CARSEL_STATE_DIR/last-deploy.json" ] && { echo "      ✘ --check 不该写 blocked 状态"; fail=1; }
grep -q "compose build" "$DOCKER_TRACE" && { echo "      ✘ --check 不该构建"; fail=1; }
echo

echo
if [ "$fail" -eq 0 ]; then echo "全部用例通过"; else echo "有用例未通过"; fi
exit "$fail"
