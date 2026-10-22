"""更新器 ADD / INVALIDATE / NOOP（FunctionModel 确定性），双时间轴、出处、幂等、乐观锁。"""
import pytest
from sqlalchemy import text

from memory_agent import llm
from memory_agent.worker import run_once
from tests.conftest import at
from tests.fakes import echo_answerer, fixed_updater, gold_extractor, rule_updater
from tests.helpers import F, facts, job, say, space

G = {
    "TapOn 二面下周二下午": [F("TapOn 二面", "时间", "2026-10-27 下午", "TapOn 二面在 2026-10-27（周二）下午", time_expression="下周二下午", resolved_date="2026-10-27")],
    "二面改到周四了": [F("TapOn 二面", "时间", "2026-10-29 下午", "TapOn 二面改到 2026-10-29（周四）下午", time_expression="周四", resolved_date="2026-10-29")],
    "再确认下，TapOn 二面是周二下午": [F("TapOn 二面", "时间", "2026-10-27 下午", "TapOn 二面在 2026-10-27（周二）下午")],
    "我喜欢做后端": [F("我", "喜欢的方向", "后端", "用户喜欢做后端")],
    "我也喜欢做 AI 应用": [F("我", "喜欢的方向", "AI 应用", "用户也喜欢做 AI 应用")],
}


@pytest.fixture
def models():
    with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
        yield


def test_add_has_provenance_and_bitemporal(engine, owner, models):
    s = space(engine, owner)
    r = say(engine, s, "TapOn 二面下周二下午", at(23))
    (f,) = facts(engine, s)
    assert f["status"] == "active" and f["source_episode_ids"] == [r["episode_id"]]
    assert f["valid_from"] == at(23) and f["valid_to"] is None and f["recorded_at"] is not None and f["expired_at"] is None
    assert f["lineage_id"] == f["id"] and f["version"] == 1
    with engine.connect() as c:
        ops = c.execute(text("SELECT op, model FROM memory_ops WHERE fact_id=:i"), {"i": f["id"]}).all()
    assert ops == [("ADD", "rule:no-candidates")]


def test_invalidate_keeps_history(engine, owner, models):
    s = space(engine, owner)
    say(engine, s, "TapOn 二面下周二下午", at(23))
    say(engine, s, "二面改到周四了", at(24))
    old, new = facts(engine, s)
    assert old["status"] == "superseded" and old["valid_to"] == new["valid_from"] == at(24) and old["expired_at"] is not None
    assert new["status"] == "active" and new["supersedes_id"] == old["id"] and new["lineage_id"] == old["lineage_id"]
    with engine.connect() as c:
        ops = [r[0] for r in c.execute(text("SELECT op FROM memory_ops WHERE space_id=:s AND op<>'REDACT' ORDER BY id"), {"s": s})]
    assert ops == ["ADD", "INVALIDATE", "ADD"]


def test_noop_only_confirms(engine, owner, models):
    s = space(engine, owner)
    say(engine, s, "TapOn 二面下周二下午", at(23))
    r = say(engine, s, "再确认下，TapOn 二面是周二下午", at(25))
    (f,) = facts(engine, s)
    assert f["status"] == "active" and f["last_confirmed_at"] == at(25)
    assert r["episode_id"] in f["source_episode_ids"]


def test_compatible_preferences_are_both_added(engine, owner, models):
    s = space(engine, owner)
    say(engine, s, "我喜欢做后端", at(23))
    say(engine, s, "我也喜欢做 AI 应用", at(24))
    assert len(facts(engine, s, "active")) == 2


def test_invalid_target_from_model_falls_back_to_add(engine, owner):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
    with llm.use_models(extract=gold_extractor(G), update=fixed_updater({"op": "INVALIDATE", "target_fact_id": 999999, "reason": "x"})):
        say(engine, s, "二面改到周四了", at(24))
    assert len(facts(engine, s, "active")) == 2  # 不会作废不存在/无关的事实


def test_retry_does_not_duplicate_facts(engine, owner, models):
    s = space(engine, owner)
    r = say(engine, s, "TapOn 二面下周二下午", at(23))
    with engine.begin() as c:  # 模拟作业被重复执行（如租约过期后重领）
        c.execute(text("UPDATE jobs SET status='queued', run_after=now() WHERE id=:i"), {"i": r["job_id"]})
    run_once(engine)
    assert len(facts(engine, s)) == 1


def test_optimistic_lock_conflict_rolls_back_and_retries(engine, owner):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
    bumped = {"done": False}

    def concurrent_edit(data):  # 决策期间另一个写者修改了目标事实
        if not bumped["done"]:
            bumped["done"] = True
            with engine.begin() as c:
                c.execute(text("UPDATE facts SET version=version+1 WHERE id=:i"), {"i": data["existing"][0]["id"]})

    with llm.use_models(extract=gold_extractor(G), update=rule_updater(on_call=concurrent_edit)):
        from memory_agent.store import ingest_episode
        with engine.begin() as c:
            r = ingest_episode(c, space_id=s, session_id="s1", content="二面改到周四了", occurred_at=at(24))
        run_once(engine)
        j = job(engine, r["job_id"])
        assert j["status"] == "queued" and "ConcurrentUpdate" in j["last_error"]
        assert [f["status"] for f in facts(engine, s)] == ["active"]  # 整个作业回滚
        run_once(engine)  # 重试整个决策
    assert job(engine, r["job_id"])["status"] == "done"
    assert [f["status"] for f in facts(engine, s)] == ["superseded", "active"]
