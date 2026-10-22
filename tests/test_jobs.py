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
