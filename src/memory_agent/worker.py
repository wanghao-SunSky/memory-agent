"""worker：循环领取作业。`python -m memory_agent.worker`。"""
from __future__ import annotations

import logging
import signal
import threading

from . import jobs
from .db import get_engine
from .engine import process_extract_job

log = logging.getLogger("memory_agent.worker")
HANDLERS = {"extract": process_extract_job}


def run_once(engine=None, worker_id: str = jobs.WORKER_ID, *, job_id: int | None = None) -> dict | None:
    engine = engine or get_engine()
    job = jobs.claim(engine, worker_id, job_id=job_id)
    if job is None:
        return None
    try:
        HANDLERS[job["kind"]](engine, job)
        log.info("job %s done", job["id"])
    except Exception as e:  # 失败已在 handler 里登记退避/死信
        log.warning("job %s failed attempt %s: %s", job["id"], job["attempts"], type(e).__name__)
    return job


def run_until_idle(engine=None, max_jobs: int = 1000) -> int:
    n = 0
    while n < max_jobs and run_once(engine) is not None:
        n += 1
    return n


def main() -> None:
    from .tracing import setup_tracing, force_flush

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    setup_tracing("memory-agent-worker")
    stop = threading.Event()

    def _stop(*_):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    engine = get_engine()

    def _heartbeat():
        while not stop.is_set():
            try:
                jobs.heartbeat(engine)
            except Exception as exc:
                log.warning("heartbeat failed: %s", type(exc).__name__)
            stop.wait(5)

    pulse = threading.Thread(target=_heartbeat, name="worker-heartbeat", daemon=True)
    pulse.start()
    log.info("worker %s started", jobs.WORKER_ID)
    try:
        while not stop.is_set():
            try:
                if run_once(engine) is None:
                    stop.wait(0.3)
            except Exception as exc:
                log.warning("job polling failed: %s", type(exc).__name__)
                stop.wait(2)
            force_flush()
    finally:
        stop.set()
        pulse.join(timeout=6)
        try:
            from sqlalchemy import text
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM worker_heartbeats WHERE worker_id=:w"), {"w": jobs.WORKER_ID})
        finally:
            force_flush()


if __name__ == "__main__":
    main()
