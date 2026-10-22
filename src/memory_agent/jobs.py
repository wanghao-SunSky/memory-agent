"""Postgres 作业队列：SKIP LOCKED 领取、租约、指数退避、死信。"""
from __future__ import annotations

import json
import os
import socket

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .redact import redact

LEASE_SECONDS = int(os.environ.get("JOB_LEASE_SECONDS", "120"))
BACKOFF_BASE = float(os.environ.get("JOB_BACKOFF_BASE", "2"))
BACKOFF_CAP = 300.0
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def claim(engine: Engine, worker_id: str = WORKER_ID, kinds: tuple[str, ...] = ("extract",)) -> dict | None:
    """领取一条可运行作业（排队中且到期，或租约过期的 running）。领取本身单独提交。"""
    with engine.begin() as conn:
        row = conn.execute(
            text("""UPDATE jobs SET status='running', attempts=attempts+1, locked_by=:w,
                           locked_until=now() + make_interval(secs => :lease), updated_at=now()
                    WHERE id = (
                        SELECT id FROM jobs
                        WHERE kind = ANY(:kinds) AND (
                              (status='queued' AND run_after <= now())
                           OR (status='running' AND locked_until < now()))
                        ORDER BY run_after, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1)
                    RETURNING id, kind, payload, attempts, max_attempts, idempotency_key"""),
            {"w": worker_id, "lease": LEASE_SECONDS, "kinds": list(kinds)},
        ).mappings().first()
        return dict(row) if row else None


def mark_done(conn: Connection, job_id: int, result: dict, trace_id: str | None) -> None:
    conn.execute(
        text("""UPDATE jobs SET status='done', finished_at=now(), updated_at=now(), locked_by=NULL, locked_until=NULL,
                       last_error=NULL, result=CAST(:r AS jsonb), trace_id=:t WHERE id=:id"""),
        {"id": job_id, "r": json.dumps(result, ensure_ascii=False), "t": trace_id},
    )


def mark_failed(engine: Engine, job: dict, err: BaseException, trace_id: str | None) -> str:
    msg, _ = redact(f"{type(err).__name__}: {str(err)[:300]}")
    attempts, max_attempts = job["attempts"], job["max_attempts"]
    delay = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempts - 1)))
    status = "dead" if attempts >= max_attempts else "queued"
    with engine.begin() as conn:
        conn.execute(
            text("""UPDATE jobs SET status=:st, last_error=:e, run_after=now() + make_interval(secs => :d),
                           locked_by=NULL, locked_until=NULL, updated_at=now(), trace_id=:t,
                           finished_at = CASE WHEN :st='dead' THEN now() ELSE NULL END
                    WHERE id=:id"""),
            {"st": status, "e": msg, "d": delay, "id": job["id"], "t": trace_id},
        )
    return status


def requeue_dead(conn: Connection, job_id: int) -> None:
    conn.execute(text("UPDATE jobs SET status='queued', attempts=0, run_after=now(), finished_at=NULL WHERE id=:i AND status='dead'"), {"i": job_id})


def get_job(conn: Connection, job_id: int) -> dict | None:
    r = conn.execute(text("SELECT id, kind, status, attempts, max_attempts, run_after, last_error, trace_id, result FROM jobs WHERE id=:i"), {"i": job_id}).mappings().first()
    return dict(r) if r else None
