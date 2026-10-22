"""worker：循环领取作业。`python -m memory_agent.worker`。"""
from __future__ import annotations

import logging
import signal
import time

from . import jobs
from .db import get_engine
from .engine import process_extract_job

log = logging.getLogger("memory_agent.worker")
HANDLERS = {"extract": process_extract_job}


def run_once(engine=None, worker_id: str = jobs.WORKER_ID) -> dict | None:
    engine = engine or get_engine()
    job = jobs.claim(engine, worker_id)
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
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker %s started", jobs.WORKER_ID)
    while not stop:
        if run_once() is None:
            time.sleep(0.3)
        force_flush()
    force_flush()


if __name__ == "__main__":
    main()
