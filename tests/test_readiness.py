from fastapi.testclient import TestClient
from sqlalchemy import text

from memory_agent import api, jobs
from memory_agent.api import create_app


def test_readiness_requires_fresh_worker(engine, monkeypatch):
    monkeypatch.setenv('MEMORY_MODEL_MODE', 'demo')
    c = TestClient(create_app(instrument=False, allow_test_auth=True))
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM worker_heartbeats'))
    assert c.get('/livez').status_code == 200
    missing = c.get('/readyz')
    assert missing.status_code == 503 and missing.json()['checks']['worker'] is False
    jobs.heartbeat(engine, 'test-ready')
    assert c.get('/readyz').status_code == 200
    with engine.begin() as conn:
        conn.execute(text("UPDATE worker_heartbeats SET seen_at=now()-interval '31 seconds'"))
    assert c.get('/readyz').status_code == 503


def test_live_readiness_requires_key(engine, monkeypatch):
    monkeypatch.setenv('MEMORY_MODEL_MODE', 'live')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(api.config, 'load_secrets', lambda: None)
    jobs.heartbeat(engine, 'test-live-ready')
    c = TestClient(create_app(instrument=False, allow_test_auth=True))
    result = c.get('/readyz')
    assert result.status_code == 503 and result.json()['checks']['model'] is False
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key-only')
    assert c.get('/readyz').status_code == 200


def test_readiness_reports_database_failure_without_details(monkeypatch):
    def broken():
        raise RuntimeError('SECRET-CONNECTION-URL')
    monkeypatch.setattr(api, 'get_engine', broken)
    c = TestClient(create_app(instrument=False, allow_test_auth=True))
    result = c.get('/healthz')
    assert result.status_code == 503 and result.json()['checks']['database'] is False
    assert 'SECRET-CONNECTION-URL' not in result.text


def test_heartbeat_does_not_block_on_busy_job(engine, owner):
    from tests.helpers import space
    from tests.conftest import at
    from memory_agent.store import ingest_episode
    sid = space(engine, owner)
    with engine.begin() as conn:
        j = ingest_episode(conn, space_id=sid, session_id='busy', content='忙碌任务', occurred_at=at(23))
    claimed = jobs.claim(engine, 'test-busy')
    assert claimed['id'] == j['job_id']
    with engine.begin() as locked:
        locked.execute(text('SELECT id FROM jobs WHERE id=:i FOR UPDATE'), {'i': j['job_id']})
        jobs.heartbeat(engine, 'test-busy')
        with engine.connect() as read:
            assert read.execute(text("SELECT count(*) FROM worker_heartbeats WHERE worker_id='test-busy'")).scalar_one() == 1
