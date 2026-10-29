#!/usr/bin/env bash
# 原生 Apple Silicon 开发环境；不改系统数据库，不需要 sudo。
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo '此安装器支持 Apple Silicon macOS；其他系统请使用 scripts/docker.sh start。' >&2
  exit 1
fi
if ! xcrun --find clang >/dev/null 2>&1; then
  echo '缺少 Apple 编译工具。请先运行 xcode-select --install，安装后重新执行此脚本。' >&2
  exit 1
fi
mkdir -p .local/build .local/logs
if ! command -v uv >/dev/null 2>&1; then
  curl --fail --location --silent --show-error https://astral.sh/uv/0.11.7/install.sh -o .local/build/install-uv.sh
  sh .local/build/install-uv.sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv python install cpython-3.12-macos-aarch64-none
uv sync --locked --python cpython-3.12-macos-aarch64-none
export MEMORY_PROJECT_ROOT="$PWD"
.venv/bin/python - <<'PY'
import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import urllib.request

root = Path(os.environ['MEMORY_PROJECT_ROOT'])
build = root / '.local/build'
install = root / '.local/pgsql'
packages = [
    ('postgresql-16.15', 'https://ftp.postgresql.org/pub/source/v16.15/postgresql-16.15.tar.bz2', 'c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed'),
    ('pgvector-0.8.7', 'https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.7.tar.gz', 'cac0b10c360f05b2d521200105ba3697e773d4cd3731f5a915a7e37ebe0bea85'),
]
for name, url, sha in packages:
    marker = install / ('bin/pg_ctl' if name.startswith('postgresql') else 'share/extension/vector.control')
    if marker.exists():
        print(f'{name}: already installed', flush=True)
        continue
    archive = build / url.rsplit('/', 1)[1]
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != sha:
        temporary = archive.with_suffix(archive.suffix + '.download')
        print(f'Downloading {name}', flush=True)
        urllib.request.urlretrieve(url, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != sha:
            raise SystemExit(f'Checksum mismatch: {name}')
        temporary.replace(archive)
    with tarfile.open(archive) as source:
        source.extractall(build, filter='data')
    commands = ([['./configure', f'--prefix={install}', '--without-icu', '--without-readline', '--without-zlib'], ['make', '-j4'], ['make', 'install']]
                if name.startswith('postgresql') else
                [['make', '-j4', f'PG_CONFIG={install}/bin/pg_config'], ['make', 'install', f'PG_CONFIG={install}/bin/pg_config']])
    logfile = root / f'.local/logs/{name}-build.log'
    with logfile.open('w') as log:
        for cmd in commands:
            print('Building', name, cmd[0], flush=True)
            subprocess.run(cmd, cwd=build/name, stdout=log, stderr=subprocess.STDOUT, check=True)
print('Database binaries ready.', flush=True)
PY
scripts/local.sh init
printf '%s\n' '初始化完成。启动：scripts/local.sh start；测试：scripts/local.sh test -q。'
