#!/usr/bin/env sh
set -eu
# Compose starts worker only after API migrations and /livez succeed.
case "${1:-api}" in
  api)
    python -m memory_agent.migrate
    exec python -m uvicorn --factory memory_agent.api:build --host 0.0.0.0 --port 8000
    ;;
  worker) exec python -m memory_agent.worker ;;
  *) exec "$@" ;;
esac
