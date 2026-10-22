#!/usr/bin/env bash
# 启动依赖（本机 Postgres 16 + Jaeger）+ 迁移 + API + worker。用法：scripts/start.sh [--fg]
set -euo pipefail
cd "$(dirname "$0")/.."
JAEGER_BIN="${JAEGER_BIN:-/home/box/opt/jaeger/jaeger-2.22.0-linux-amd64/jaeger}"
LOG_DIR="${LOG_DIR:-/tmp/memory-agent}"; mkdir -p "$LOG_DIR"
PORT="${PORT:-8000}"
export OTEL_BSP_SCHEDULE_DELAY="${OTEL_BSP_SCHEDULE_DELAY:-1000}"  # trace 1 秒内导出到 Jaeger

if ! pg_lsclusters 2>/dev/null | grep -q "^16 .*online"; then sudo pg_ctlcluster 16 main start; fi
if ! curl -sf -o /dev/null http://localhost:16686/; then
  nohup "$JAEGER_BIN" > "$LOG_DIR/jaeger.log" 2>&1 &
  for _ in $(seq 1 30); do curl -sf -o /dev/null http://localhost:16686/ && break; sleep 0.5; done
fi
uv run python -m memory_agent.migrate

if ! curl -sf -o /dev/null "http://localhost:$PORT/healthz"; then
  nohup uv run uvicorn --factory memory_agent.api:build --host 127.0.0.1 --port "$PORT" > "$LOG_DIR/api.log" 2>&1 &
  echo $! > "$LOG_DIR/api.pid"
fi
if [ ! -f "$LOG_DIR/worker.pid" ] || ! kill -0 "$(cat "$LOG_DIR/worker.pid")" 2>/dev/null; then
  nohup uv run python -m memory_agent.worker > "$LOG_DIR/worker.log" 2>&1 &
  echo $! > "$LOG_DIR/worker.pid"
fi
for _ in $(seq 1 60); do curl -sf -o /dev/null "http://localhost:$PORT/healthz" && break; sleep 0.5; done
echo "API:    http://localhost:$PORT   （你记得我什么：http://localhost:$PORT/?space=work）"
echo "Jaeger: http://localhost:16686"
echo "日志:   $LOG_DIR"
