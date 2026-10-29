#!/usr/bin/env bash
# 兼容旧入口；新机器先 scripts/bootstrap.sh，容器部署用 docker compose up --build。
set -euo pipefail
exec "$(dirname "$0")/local.sh" start "$@"
