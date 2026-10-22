#!/usr/bin/env bash
LOG_DIR="${LOG_DIR:-/tmp/memory-agent}"
for p in api worker; do
  [ -f "$LOG_DIR/$p.pid" ] && kill "$(cat "$LOG_DIR/$p.pid")" 2>/dev/null && rm -f "$LOG_DIR/$p.pid" && echo "stopped $p"
done
pkill -f "uvicorn --factory memory_agent.api:build" 2>/dev/null || true
pkill -f "python -m memory_agent.worker" 2>/dev/null || true
