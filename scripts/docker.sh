#!/usr/bin/env bash
# One-command container demo; random bearer token stays in ignored .local/.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null 2>&1 || { echo '请先安装并启动 Docker（含 Compose v2）。' >&2; exit 1; }
mkdir -p .local
CONFIG=.local/docker.env
if [ ! -s "$CONFIG" ]; then
  umask 077
  TOKEN="$(openssl rand -hex 32)"
  printf "MEMORY_API_TOKENS='{\"%s\":\"demo\"}'\n" "$TOKEN" > "$CONFIG"
fi
case "${1:-start}" in
  start)
    docker compose --env-file "$CONFIG" up --build -d
    for _ in {1..120}; do
      if curl --noproxy '*' -fsS http://127.0.0.1:8000/readyz >/dev/null 2>&1; then
        printf '%s\n' '已就绪：http://127.0.0.1:8000/；运行 scripts/docker.sh token 查看访问令牌。'
        exit 0
      fi
      sleep 1
    done
    docker compose --env-file "$CONFIG" logs --tail 30
    echo '服务未就绪，请检查日志。' >&2
    exit 1
    ;;
  token) sed -E 's/^MEMORY_API_TOKENS=.\{"([^"]+)".*$/\1/' "$CONFIG" ;;
  stop) docker compose --env-file "$CONFIG" down ;;
  logs) docker compose --env-file "$CONFIG" logs -f --tail 100 ;;
  status) docker compose --env-file "$CONFIG" ps ;;
  *) echo '用法: scripts/docker.sh {start|token|stop|logs|status}' >&2; exit 2 ;;
esac
