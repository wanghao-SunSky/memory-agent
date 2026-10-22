"""极简迁移器：按文件名顺序执行 migrations/*.sql，记录在 schema_migrations。"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from .db import get_engine

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def migrate() -> list[str]:
    applied_now: list[str] = []
    with get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"))
        done = {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations"))}
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if f.name in done:
                continue
            conn.exec_driver_sql(f.read_text(encoding="utf-8"))
            conn.execute(text("INSERT INTO schema_migrations(name) VALUES (:n)"), {"n": f.name})
            applied_now.append(f.name)
    return applied_now


if __name__ == "__main__":
    print("applied:", migrate() or "nothing new")
