#!/bin/bash
# 用 Git Bash 在本机**真跑** carsel-deploy.sh 的 --check 路径。
#
# 目的：验证 2026-10-05 新加的三处（变量拼接 / 取 commit_sha / 不一致告警）
# 不只是 `bash -n` 的语法对，而是**真能跑出预期输出**。
#
# 全程在临时目录内 + 打桩 curl：不调真 GitHub API、不碰真服务器、不写真 /var。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/deploy/carsel-deploy.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export CARSEL_STATE_DIR="$TMP/state"
export CARSEL_DEPLOY_LOG="$TMP/deploy.log"
mkdir -p "$CARSEL_STATE_DIR"

A40=$(printf 'a%.0s' $(seq 40))
B40=$(printf 'b%.0s' $(seq 40))
C40=$(printf 'c%.0s' $(seq 40))

mkdir -p "$TMP/bin"
cat > "$TMP/bin/curl" <<'STUB'
#!/bin/bash
# 打桩 curl：按 URL 分支返回，不发任何真实请求
for a in "$@"; do
  case "$a" in
    *api.github.com*commits*)
      echo "{\"sha\":\"$FAKE_REMOTE_SHA\"}"; exit 0 ;;
    */api/v1/version)
      echo "STUB_HIT_VERSION_URL: $a" >> "$CURL_TRACE"
      echo "{\"app_version\":\"0.1.0\",\"commit_sha\":\"$FAKE_RUNNING_SHA\",\"build_time\":\"x\"}"
      exit 0 ;;
  esac
done
exit 0
STUB
chmod +x "$TMP/bin/curl"
export PATH="$TMP/bin:$PATH"

export CURL_TRACE="$TMP/curl.trace"
fail=0

run_case() {
  local desc="$1" deployed="$2" running="$3" want="$4"
  : > "$CURL_TRACE"
  printf '%s' "$deployed" > "$CARSEL_STATE_DIR/deployed-main.sha"
  local out
  out=$(FAKE_REMOTE_SHA="$A40" FAKE_RUNNING_SHA="$running" bash "$SCRIPT" --check 2>&1)
  echo "--- $desc"
  echo "$out" | sed 's/^/      /'
  if echo "$out" | grep -q "$want"; then
    echo "      ✔ 命中预期：$want"
  else
    echo "      ✘ 未命中预期：$want"
    fail=1
  fi
  echo
}

run_case "用例1 状态文件==main，容器同一 SHA → 应「已是最新」且无告警" \
  "$A40" "$A40" "已是最新"
run_case "用例1b 同上但容器自报 unknown → 仍应「已是最新」，且不误报不一致" \
  "$A40" "unknown" "已是最新"
run_case "用例2 状态文件==main 但容器是另一个 SHA → 应出 ⚠️ 告警" \
  "$A40" "$B40" "运行中的容器与 main 不一致"
run_case "用例3 状态文件落后 main → 应「有更新待部署」" \
  "$C40" "$A40" "有更新待部署"

echo "=== 拼接验证：脚本实际请求的 /version URL ==="
printf '%s' "$A40" > "$CARSEL_STATE_DIR/deployed-main.sha"
FAKE_REMOTE_SHA="$A40" FAKE_RUNNING_SHA="$A40" bash "$SCRIPT" --check >/dev/null 2>&1
grep 'STUB_HIT_VERSION_URL' "$CURL_TRACE" | sed 's/STUB_HIT_VERSION_URL: /    /'
if grep -q 'http://127.0.0.1:8000/api/v1/version' "$CURL_TRACE"; then
  echo "    ✔ URL 拼接正确"
else
  echo "    ✘ URL 拼接错误（期望 http://127.0.0.1:8000/api/v1/version）"
  fail=1
fi

echo
if [ "$fail" -eq 0 ]; then echo "全部用例通过"; else echo "有用例未通过"; fi
exit "$fail"
