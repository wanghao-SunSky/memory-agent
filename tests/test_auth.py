import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from memory_agent.api import create_app
from memory_agent.store import ingest_episode
from tests.conftest import at
from tests.helpers import space

TOKEN_A = 'token-alice-test-123456789'
TOKEN_B = 'token-bob-test-123456789'


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv('MEMORY_AUTH_MODE', 'token')
    monkeypatch.setenv('MEMORY_API_TOKENS', json.dumps({TOKEN_A: 'alice-test', TOKEN_B: 'bob-test'}))
    return TestClient(create_app(instrument=False))


def test_missing_or_invalid_token_is_rejected(protected):
    assert protected.get('/memories').status_code == 401
    assert protected.get('/memories', headers={'Authorization': 'Bearer wrong', 'X-Owner-Id': 'alice-test'}).status_code == 401
    assert protected.get('/me', headers={'Authorization': 'Bearer ' + TOKEN_A, 'X-Owner-Id': 'bob-test'}).json() == {'owner': 'alice-test'}


def test_job_and_memory_data_are_scoped_to_authenticated_owner(protected, engine):
    alice, bob = space(engine, 'alice-test'), space(engine, 'bob-test')
    with engine.begin() as conn:
        job = ingest_episode(conn, space_id=alice, session_id='auth', content='属于 Alice 的安排', occurred_at=at(23))
    path = f"/jobs/{job['job_id']}"
    assert protected.get(path, headers={'Authorization': 'Bearer ' + TOKEN_A}).status_code == 200
    assert protected.get(path, headers={'Authorization': 'Bearer ' + TOKEN_B, 'X-Owner-Id': 'alice-test'}).status_code == 404
    assert protected.post(path + '/retry', headers={'Authorization': 'Bearer ' + TOKEN_B}).status_code == 404
    assert protected.get('/memories', headers={'Authorization': 'Bearer ' + TOKEN_B}).json()['memories'] == []


def test_local_identity_is_fixed_and_rebinding_and_cross_site_writes_are_blocked(monkeypatch):
    monkeypatch.setenv('MEMORY_AUTH_MODE', 'local')
    monkeypatch.setenv('MEMORY_LOCAL_OWNER', 'demo')
    local = TestClient(create_app(instrument=False), base_url='http://127.0.0.1:8000', client=('127.0.0.1', 1234))
    assert local.get('/me', headers={'X-Owner-Id': 'victim'}).json() == {'owner': 'demo'}
    assert local.post('/episodes', headers={'Origin': 'https://attacker.example'}, json={'content': 'test', 'occurred_at': '2026-10-23T10:00:00+08:00'}).status_code == 403
    assert local.get('/me', headers={'Host': 'attacker.example'}).status_code == 403
    remote = TestClient(create_app(instrument=False), base_url='http://127.0.0.1:8000', client=('192.0.2.1', 1234))
    assert remote.get('/me').status_code == 403


def test_test_auth_requires_explicit_factory_flag(monkeypatch):
    monkeypatch.setenv('MEMORY_AUTH_MODE', 'test')
    assert TestClient(create_app(instrument=False)).get('/me', headers={'X-Owner-Id': 'victim'}).status_code == 503
    assert TestClient(create_app(instrument=False, allow_test_auth=True)).get('/me', headers={'X-Owner-Id': 'fixture'}).json() == {'owner': 'fixture'}


def test_private_content_not_embedded_in_html_or_validation_errors(protected):
    script = '</script><script>alert("probe")</script>'
    page = protected.get('/', params={'owner': script}).text
    assert script not in page
    assert 'localStorage' not in page
    invalid = protected.post('/episodes', headers={'Authorization': 'Bearer ' + TOKEN_A}, json={'content': 'SECRET-PROBE', 'occurred_at': '2026-10-23T10:00:00'})
    assert invalid.status_code == 422
    assert 'SECRET-PROBE' not in invalid.text
    assert 'input' not in invalid.json()['detail'][0]


def test_job_errors_do_not_echo_provider_text(protected, engine):
    sid = space(engine, 'alice-test')
    with engine.begin() as conn:
        j = ingest_episode(conn, space_id=sid, session_id='auth-error', content='失败记录', occurred_at=at(23))
        conn.execute(text("UPDATE jobs SET status='dead',last_error='SECRET-PROVIDER-PROBE' WHERE id=:i"), {'i': j['job_id']})
    out = protected.get(f"/jobs/{j['job_id']}", headers={'Authorization': 'Bearer ' + TOKEN_A})
    assert out.status_code == 200 and 'SECRET-PROVIDER-PROBE' not in out.text
    retry = protected.post(f"/jobs/{j['job_id']}/retry", headers={'Authorization': 'Bearer ' + TOKEN_A})
    assert retry.status_code == 200 and retry.json()['status'] == 'queued'
