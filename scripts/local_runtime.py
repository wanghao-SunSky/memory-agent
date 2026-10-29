"""Local-only lifecycle. Read dotenv as data; never execute its contents as shell code."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
LOGS = LOCAL / 'logs'
PG = LOCAL / 'pgsql/bin'
DATA = LOCAL / 'pgdata'
SOCKET = LOCAL / 'socket'
RUNTIME_URL = 'postgresql+psycopg://memory_local@127.0.0.1:55432/memory_learning'
TEST_URL = 'postgresql+psycopg://memory_test_local@127.0.0.1:55432/memory_test_learning'


def environment() -> dict[str, str]:
    load_dotenv(ROOT / '.env.local', override=False)
    env = dict(os.environ)
    env.update(DATABASE_URL=RUNTIME_URL, FASTEMBED_CACHE_PATH=str(LOCAL / 'models'),
               MEMORY_SECRETS_FILE=str(ROOT / '.env.local'))
    env.setdefault('MEMORY_MODEL_MODE', 'demo')
    env.setdefault('MEMORY_AUTH_MODE', 'local')
    env.setdefault('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', '')
    # HTTP(S) proxy is sufficient; httpx has no optional socksio dependency.
    env.pop('ALL_PROXY', None)
    env.pop('all_proxy', None)
    env['NO_PROXY'] = 'localhost,127.0.0.1,::1,' + env.get('NO_PROXY', '')
    env['no_proxy'] = env['NO_PROXY']
    return env


ENV = environment()
PORT = int(ENV.get('MEMORY_PORT', '8000'))
PYTHON = str(ROOT / '.venv/bin/python')


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, env=ENV, check=True, **kwargs)


def pg_run(name: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return run([str(PG / name), *args], **kwargs)


def db_start() -> None:
    for path in (LOGS, SOCKET):
        path.mkdir(parents=True, exist_ok=True)
    if not (DATA / 'PG_VERSION').exists():
        with (LOGS / 'initdb.log').open('w') as log:
            pg_run('initdb', '-D', str(DATA), '-U', 'memory_admin', '--encoding=UTF8', '--locale=C', '--auth=trust', stdout=log)
        escaped_socket = str(SOCKET).replace("'", "''")
        with (DATA / 'postgresql.conf').open('a') as f:
            f.write(f"\nlisten_addresses = '127.0.0.1'\nport = 55432\nunix_socket_directories = '{escaped_socket}'\n")
    status = subprocess.run([str(PG / 'pg_ctl'), '-D', str(DATA), 'status'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if status.returncode:
        pg_run('pg_ctl', '-D', str(DATA), '-l', str(LOGS / 'postgres.log'), '-w', 'start')


def sql(database: str, statement: str) -> str:
    return pg_run('psql', '-X', '-v', 'ON_ERROR_STOP=1', '-h', str(SOCKET), '-p', '55432',
                  '-U', 'memory_admin', '-d', database, '-Atc', statement,
                  capture_output=True, text=True).stdout.strip()


def init() -> None:
    db_start()
    for role, database in [('memory_local', 'memory_learning'), ('memory_test_local', 'memory_test_learning')]:
        if sql('postgres', f"SELECT count(*) FROM pg_roles WHERE rolname='{role}'") == '0':
            sql('postgres', f'CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
        if sql('postgres', f"SELECT count(*) FROM pg_database WHERE datname='{database}'") == '0':
            sql('postgres', f'CREATE DATABASE {database} OWNER {role}')
        sql(database, f'CREATE EXTENSION IF NOT EXISTS vector; REVOKE CONNECT ON DATABASE {database} FROM PUBLIC; GRANT CONNECT ON DATABASE {database} TO {role}')
    run([PYTHON, '-m', 'memory_agent.migrate'])
    subprocess.run([PYTHON, '-m', 'memory_agent.migrate'], cwd=ROOT, env=ENV | {'DATABASE_URL': TEST_URL}, check=True)


def owned_pid(name: str) -> int | None:
    pidfile = LOGS / f'{name}.pid'
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
        if pid <= 1:
            raise ValueError('invalid PID')
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, check=False)
        if result.returncode:
            pidfile.unlink()
            return None
        expected = 'memory_agent.api:build' if name == 'api' else 'memory_agent.worker'
        if str(ROOT / '.venv/bin/python') not in result.stdout or expected not in result.stdout:
            raise RuntimeError(f'{name} PID 已被其他进程使用；未发送信号。请检查 {pidfile}')
        return pid
    except ValueError as e:
        raise RuntimeError(f'无效进程记录：{pidfile}') from e


def stop() -> None:
    for name in ('api', 'worker'):
        pid = owned_pid(name)
        if pid is None:
            continue
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if owned_pid(name) is None:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f'{name} 尚未退出，保留 PID 记录，请检查日志后重试。')
        (LOGS / f'{name}.pid').unlink(missing_ok=True)
        print(f'{name}: stopped')


def ready() -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{PORT}/readyz', timeout=2) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)
        except ValueError:
            detail = {'status': e.code}
        raise RuntimeError(json.dumps(detail, ensure_ascii=False)) from None


def start() -> None:
    init()
    # Prefetch/cache the real local embedding model before reporting readiness.
    run([PYTHON, '-c', 'from memory_agent.embedding import embed_query; print("embedding:", len(embed_query("健康检查")))'])
    for name, args in [('api', ['-m', 'uvicorn', '--factory', 'memory_agent.api:build', '--host', '127.0.0.1', '--port', str(PORT)]),
                       ('worker', ['-m', 'memory_agent.worker'])]:
        if owned_pid(name) is not None:
            print(f'{name}: already running (配置变化需 restart)')
            continue
        if name == 'api':
            with socket.socket() as s:
                s.bind(('127.0.0.1', PORT))
        with (LOGS / f'{name}.log').open('ab') as log:
            p = subprocess.Popen([PYTHON, *args], cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True)
        (LOGS / f'{name}.pid').write_text(str(p.pid))
        print(f'{name}: started ({p.pid})')
    problem = ''
    for _ in range(90):
        try:
            info = ready()
            print(json.dumps(info, ensure_ascii=False))
            print(f'API: http://127.0.0.1:{PORT}/  Docs: http://127.0.0.1:{PORT}/docs')
            return
        except (OSError, ValueError, RuntimeError) as e:
            problem = str(e)
            time.sleep(0.5)
    raise RuntimeError(f'服务未就绪：{problem}；请检查 .local/logs/api.log 和 worker.log')


def test(args: list[str]) -> None:
    init()
    for name in ('offline', 'live'):
        report = ROOT / f'reports/last_{name}.json'
        if report.exists():
            import shutil
            shutil.copy2(report, LOGS / f'{name}-before-{time.time_ns()}.json')
    # Test mode is explicit, fixed to a disposable DB, independent of a running worker.
    env = ENV | {'DATABASE_URL': TEST_URL, 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': '',
                 'MEMORY_AUTH_MODE': 'test', 'MEMORY_MODEL_MODE': 'demo'}
    os.execve(PYTHON, [PYTHON, '-m', 'pytest', *args], env)


def evaluate(args: list[str]) -> None:
    init()
    env = ENV | {'DATABASE_URL': TEST_URL, 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': ''}
    os.execve(PYTHON, [PYTHON, str(ROOT / 'scripts/evaluate.py'), *args], env)


def main() -> None:
    os.chdir(ROOT)
    command, *args = sys.argv[1:] or ['status']
    if command == 'init':
        init()
    elif command == 'start':
        start()
    elif command == 'restart':
        stop()
        start()
    elif command == 'stop':
        stop()
    elif command == 'db-stop':
        stop()
        pg_run('pg_ctl', '-D', str(DATA), '-m', 'fast', '-w', 'stop')
    elif command == 'status':
        pg_run('pg_ctl', '-D', str(DATA), 'status')
        print(json.dumps(ready(), ensure_ascii=False))
        print({name: owned_pid(name) for name in ('api', 'worker')})
    elif command == 'test':
        test(args)
    elif command == 'evaluate':
        evaluate(args)
    else:
        raise SystemExit('用法: scripts/local.sh {init|start|restart|stop|db-stop|status|test [pytest 参数]|evaluate [评测参数]}')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, OSError) as e:
        print(f'错误：{e}', file=sys.stderr)
        raise SystemExit(1)
