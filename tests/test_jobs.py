import threading

from sqlalchemy import text

from memory_agent import jobs, llm
from memory_agent.worker import run_once
from tests.conftest import at
from tests.fakes import echo_answerer, gold_extractor, rule_updater
from tests.helpers import job, space
from memory_agent.store import ingest_episode
from pydantic_ai.models.function import FunctionModel


def _enqueue(engine, s, n):
    ids = []
    with engine.begin() as c:
        for i in range(n):
            ids.append(ingest_episode(c, space_id=s, session_id="s", content=f"第{i}条", occurred_at=at(23))["job_id"])
    return ids


def test_skip_locked_each_job_claimed_once(engine, owner):
    s = space(engine, owner)
    ids = set(_enqueue(engine, s, 12))
    claimed, lock = [], threading.Lock()

    def worker(w):
        while True:
            j = jobs.claim(engine, f"w{w}")
            if j is None:
                return
            with lock:
                claimed.append(j["id"])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    mine = [i for i in claimed if i in ids]
    assert sorted(mine) == sorted(ids)  # 不丢
    assert len(mine) == len(set(mine))  # 不重


def test_locked_row_is_skipped(engine, owner):
    s = space(engine, owner)
    a, b = _enqueue(engine, s, 2)
    with engine.connect() as other:
        tx = other.begin()
        other.execute(text("SELECT id FROM jobs WHERE id=:i FOR UPDATE"), {"i": a})
        j = jobs.claim(engine, "w")
        tx.rollback()
    assert j["id"] == b


def _boom(messages, info):
    raise RuntimeError("model down")


def test_backoff_then_dead_letter(engine, owner):
    s = space(engine, owner)
    (jid,) = _enqueue(engine, s, 1)
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET max_attempts=3 WHERE id=:i"), {"i": jid})
    with llm.use_models(extract=FunctionModel(_boom)):
        for expected in ("queued", "queued", "dead"):
            assert run_once(engine)["id"] == jid
            j = job(engine, jid)
            assert j["status"] == expected and "RuntimeError" in j["last_error"]
    assert job(engine, jid)["attempts"] == 3
    assert run_once(engine) is None  # 死信不再被领取
    with engine.begin() as c:
        jobs.requeue_dead(c, jid)
    with llm.use_models(extract=gold_extractor({}), update=rule_updater(), answer=echo_answerer()):
        run_once(engine)
    assert job(engine, jid)["status"] == "done"


def test_backoff_delay_respected(engine, owner, monkeypatch):
    monkeypatch.setattr(jobs, "BACKOFF_BASE", 60)
    s = space(engine, owner)
    (jid,) = _enqueue(engine, s, 1)
    with llm.use_models(extract=FunctionModel(_boom)):
        run_once(engine)
    with engine.connect() as c:
        delay = c.execute(text("SELECT EXTRACT(EPOCH FROM run_after - now()) FROM jobs WHERE id=:i"), {"i": jid}).scalar_one()
    assert 50 < float(delay) <= 60


def test_expired_lease_is_reclaimed(engine, owner):
    s = space(engine, owner)
    (jid,) = _enqueue(engine, s, 1)
    j = jobs.claim(engine, "dead-worker")
    assert j["id"] == jid
    with engine.begin() as c:  # 模拟 worker 被杀：租约过期
        c.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id=:i"), {"i": jid})
    j2 = jobs.claim(engine, "new-worker")
    assert j2["id"] == jid and j2["attempts"] == 2


def test_targeted_claim_does_not_take_unrelated_job(engine, owner):
    a, b = _enqueue(engine, space(engine, owner), 2)
    assert jobs.claim(engine, "evaluator", job_id=b)["id"] == b
    assert job(engine, a)["status"] == "queued"
    assert jobs.claim(engine, "evaluator", job_id=b) is None


def test_stale_worker_cannot_execute_or_mark_failure(engine, owner):
    from memory_agent.engine import process_extract_job
    (jid,) = _enqueue(engine, space(engine, owner), 1)
    old = jobs.claim(engine, "old")
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET locked_until=now()-interval '1 second' WHERE id=:id"), {"id": jid})
    new = jobs.claim(engine, "new")
    assert process_extract_job(engine, old) == {"skipped": "job lease no longer owned"}
    assert jobs.mark_failed(engine, old, RuntimeError("late failure"), None) == "stale"
    assert job(engine, jid)["status"] == "running"
    assert job(engine, jid)["attempts"] == new["attempts"]


def test_executing_job_is_not_reclaimed_after_lease_expires(engine, owner, monkeypatch):
    from memory_agent import engine as processor
    (jid,) = _enqueue(engine, space(engine, owner), 1)
    claimed = jobs.claim(engine, "slow")
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET locked_until=now()-interval '1 second' WHERE id=:id"), {"id": jid})
    entered, release = threading.Event(), threading.Event()
    errors = []

    def slow_extract(*_):
        entered.set()
        assert release.wait(3)
        return [], {"input_tokens": 0, "output_tokens": 0}

    def process():
        try:
            processor.process_extract_job(engine, claimed)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(processor, "extract", slow_extract)
    thread = threading.Thread(target=process)
    thread.start()
    try:
        assert entered.wait(3)
        assert jobs.claim(engine, "competitor", job_id=jid) is None
        jobs.heartbeat(engine, "slow")  # heartbeat must also skip the executing row
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert job(engine, jid)["status"] == "done"
