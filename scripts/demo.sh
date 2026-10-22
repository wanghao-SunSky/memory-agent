#!/usr/bin/env bash
# 2 分钟演示：启动服务 → 跑 6 步 → 输出 trace 链接。用的是真实 DeepSeek。
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/start.sh
uv run python scripts/demo.py "$@"
