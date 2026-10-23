#!/bin/bash
# 断言：容器**不是以 root 运行**（README 已知待办「容器非 root 运行」的运行时护栏）。
#
# ## 为什么要有这个脚本
#
# Dockerfile 里写 `USER carsel` 只是**声明**。真正要保证的是**跑起来的时候**
# 确实是那个身份：
# - compose 里任何一处 `user:` 覆盖，声明就失效；
# - 镜像构建漏了 USER 指令（改 Dockerfile 时很容易漏），声明就失效；
# - 基础镜像换版本时默认用户变了，声明也可能失效。
#
# ⇒ 部署后当场断言一次，失败即回滚（deploy/carsel-deploy.sh 调用本脚本）。
# 「声明 + 运行时断言」两层都在，才叫护栏；只有声明那叫许愿。
#
# ## 用法
#
#   verify_non_root.sh [容器名 ...]     # 默认查 deploy-api-1 与 deploy-web-1
#
# 退出码：0 = 全部非 root；1 = 有容器以 root 运行或取不到 uid。
set -uo pipefail

CONTAINERS=("$@")
if [ "${#CONTAINERS[@]}" -eq 0 ]; then
  CONTAINERS=(deploy-api-1 deploy-web-1)
fi

fail=0
for c in "${CONTAINERS[@]}"; do
  # `id -u` 问的是**容器内当前进程的身份**（不是镜像的声明）——
  # 这正是要断言的东西。
  uid=$(docker exec "$c" id -u 2>/dev/null | tr -d '[:space:]')
  if [ -z "$uid" ]; then
    echo "✘ $c: 取不到容器内的 uid（容器未运行 / 无 id 命令 / exec 失败）"
    fail=1
    continue
  fi
  if [ "$uid" = "0" ]; then
    echo "✘ $c: 正在以 root 运行（uid=0）"
    fail=1
  else
    echo "✔ $c: uid=$uid（非 root）"
  fi
done

exit "$fail"
