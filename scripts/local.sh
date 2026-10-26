#!/usr/bin/env bash
# macOS 学习环境：独立数据库、启动和测试。安装说明见 docs/local-setup.md。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
LOCAL="$ROOT/.local"
export PATH="$LOCAL/pgsql/bin:$ROOT/.venv/bin:$PATH"
export FASTEMBED_CACHE_PATH="$LOCAL/models"
export MEMORY_SECRETS_FILE="$ROOT/.env.local"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=""
# httpx 未安装 SOCKS 扩展；保留本机 HTTP(S) 代理，仅移除兜底 SOCKS 代理。
unset ALL_PROXY all_proxy
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
export DATABASE_URL="postgresql+psycopg://memory_local@127.0.0.1:55432/memory_learning"
mkdir -p "$LOCAL/logs" "$LOCAL/socket"

db_start() {
  if [ ! -f "$LOCAL/pgdata/PG_VERSION" ]; then
    initdb -D "$LOCAL/pgdata" -U memory_admin --encoding=UTF8 --locale=C --auth=trust > "$LOCAL/logs/initdb.log"
    cat >> "$LOCAL/pgdata/postgresql.conf" <<EOF
listen_addresses = '127.0.0.1'
port = 55432
unix_socket_directories = '$LOCAL/socket'
EOF
  fi
  if ! pg_ctl -D "$LOCAL/pgdata" status >/dev/null 2>&1; then
    pg_ctl -D "$LOCAL/pgdata" -l "$LOCAL/logs/postgres.log" -w start
  fi
}

case "${1:-status}" in
  init)
    db_start
    for pair in 'memory_local memory_learning' 'memory_test_local memory_test_learning'; do
      read -r role db <<< "$pair"
      if [ "$(psql -h "$LOCAL/socket" -p 55432 -U memory_admin -d postgres -Atc "SELECT count(*) FROM pg_roles WHERE rolname='$role'")" = 0 ]; then
        createuser -h "$LOCAL/socket" -p 55432 -U memory_admin "$role"
      fi
      if [ "$(psql -h "$LOCAL/socket" -p 55432 -U memory_admin -d postgres -Atc "SELECT count(*) FROM pg_database WHERE datname='$db'")" = 0 ]; then
        createdb -h "$LOCAL/socket" -p 55432 -U memory_admin -O "$role" "$db"
      fi
      psql -X -v ON_ERROR_STOP=1 -h "$LOCAL/socket" -p 55432 -U memory_admin -d "$db" -c "CREATE EXTENSION IF NOT EXISTS vector; REVOKE CONNECT ON DATABASE $db FROM PUBLIC; GRANT CONNECT ON DATABASE $db TO $role;"
    done
    python -m memory_agent.migrate
    DATABASE_URL="postgresql+psycopg://memory_test_local@127.0.0.1:55432/memory_test_learning" python -m memory_agent.migrate
    ;;
  start)
    db_start
    python -m memory_agent.migrate
    # start_new_session 使后台服务在终端命令结束后继续运行。
    python - <<'PY'
import os, signal, socket, subprocess, sys
from pathlib import Path
root = Path.cwd()
logs = root / '.local/logs'
for name, args in [('api', ['-m', 'uvicorn', '--factory', 'memory_agent.api:build', '--host', '127.0.0.1', '--port', '8000']), ('worker', ['-m', 'memory_agent.worker'])]:
    pidfile = logs / f'{name}.pid'
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), 0)
            print(f'{name}: already running')
            continue
        except ProcessLookupError:
            pass
    if name == 'api':
        with socket.socket() as s:
            s.bind(('127.0.0.1', 8000))
    with (logs / f'{name}.log').open('ab') as log:
        p = subprocess.Popen([sys.executable, *args], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    pidfile.write_text(str(p.pid))
    print(f'{name}: started ({p.pid})')
PY
    for _ in {1..40}; do
      if curl --noproxy '*' -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
        echo 'API: http://127.0.0.1:8000/  Docs: http://127.0.0.1:8000/docs'
        exit 0
      fi
      sleep 0.5
    done
    echo 'API 启动失败，请查看 .local/logs/api.log' >&2
    exit 1
    ;;
  test)
    shift
    db_start
    export DATABASE_URL="postgresql+psycopg://memory_test_local@127.0.0.1:55432/memory_test_learning"
    # 原测试会清空测试表，入口固定到专用库，并保存旧报告。
    if [ -f reports/last_offline.json ]; then
      cp reports/last_offline.json "$LOCAL/logs/offline-before-$(date +%Y%m%d-%H%M%S).json"
    fi
    exec python -m pytest "$@"
    ;;
  stop)
    python - <<'PY'
import os, signal
from pathlib import Path
for name in ('api', 'worker'):
    p = Path('.local/logs') / f'{name}.pid'
    if p.exists():
        try:
            os.kill(int(p.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass
        p.unlink()
        print(f'{name}: stopped')
PY
    ;;
  status)
    pg_ctl -D "$LOCAL/pgdata" status
    curl --noproxy '*' -fsS http://127.0.0.1:8000/healthz
    ;;
  *) echo '用法: scripts/local.sh {init|start|test [pytest 参数]|stop|status}' >&2; exit 2 ;;
esac
