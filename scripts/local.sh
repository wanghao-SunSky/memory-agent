#!/usr/bin/env bash
# 本机开发入口：scripts/bootstrap.sh 初始化后使用。
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ] || [ ! -x .local/pgsql/bin/pg_ctl ]; then
  echo '请先运行 scripts/bootstrap.sh 初始化 Python 和数据库。' >&2
  exit 1
fi
exec .venv/bin/python scripts/local_runtime.py "$@"
