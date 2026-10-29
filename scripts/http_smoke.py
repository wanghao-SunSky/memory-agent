"""HTTP acceptance on a disposable, empty demo owner; never touches another owner's data."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get('MEMORY_SMOKE_BASE', 'http://127.0.0.1:8000')
TOKEN = os.environ.get('MEMORY_SMOKE_TOKEN', '')
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path: str, body=None, method: str | None = None):
    headers = {'Content-Type': 'application/json'}
    if TOKEN:
        headers['Authorization'] = 'Bearer ' + TOKEN
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers, method=method)
    with CLIENT.open(req, timeout=45) as r:
        return json.load(r)


def say(text: str, when: str):
    event = request('/episodes', {'space': 'work', 'session_id': 'http-smoke', 'content': text, 'occurred_at': when})
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        job = request(f'/jobs/{event["job_id"]}')
        if job['status'] == 'done':
            return job
        if job['status'] in ('dead', 'cancelled'):
            raise AssertionError(f'Job ended in {job["status"]}')
        time.sleep(0.3)
    raise AssertionError('Job timeout')


def main():
    request('/readyz')
    assert request('/info')['model_mode'] == 'demo', 'This acceptance script is limited to demo mode'
    if request('/memories')['memories']:
        raise SystemExit('Smoke requires an empty owner; use a dedicated token mapped to a fresh owner.')
    say('星河科技二面安排在2026-11-03下午', '2026-10-23T10:00:00+08:00')
    say('星河科技二面改到2026-11-05下午', '2026-10-24T10:00:00+08:00')
    answer = request('/recall', {'space': 'work', 'question': '星河科技二面什么时候？'})
    assert answer['abstain'] is False and answer['used_memory_ids'], answer
    assert '2026-11-05' in answer['answer'], answer
    past = request('/recall', {'space': 'work', 'question': '星河科技二面什么时候？', 'as_of': '2026-10-23T12:00:00+08:00'})
    assert '2026-11-03' in past['answer'], past
    agent = request('/investigate', {'space': 'work', 'question': '星河科技二面什么时候？'})
    assert agent['abstain'] is False and agent['used_memory_ids'] and agent['steps'], agent
    # Delete the created active cards one at a time, including sibling chains.
    for _ in range(20):
        memories = request('/memories')['memories']
        if not memories:
            break
        result = request('/memories/' + str(memories[0]['current']['memory_id']), method='DELETE')
        assert result['result'] == 'verified', result
    else:
        raise AssertionError('Unexpected number of demo memories')
    empty = request('/recall', {'space': 'work', 'question': '星河科技二面什么时候？'})
    assert empty['abstain'] is True and empty['used_memory_ids'] == [], empty
    print(json.dumps({'mode': 'demo', 'http_acceptance': 'passed', 'agent_steps': len(agent['steps']),
                      'checks': ['ready', 'write', 'update', 'recall', 'historical_recall', 'tools', 'delete', 'abstain']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
