from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from . import config

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(config.database_url(), pool_pre_ping=True, pool_size=10)

        @event.listens_for(_engine, "connect")
        def _register(dbapi_conn, _rec):  # noqa: ANN001
            from pgvector.psycopg import register_vector

            register_vector(dbapi_conn)

    return _engine


def reset_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
