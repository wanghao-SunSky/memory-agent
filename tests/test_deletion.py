import pytest
from sqlalchemy import text

from memory_agent import llm
from memory_agent.deletion import delete_memory
from memory_agent.recall import recall
from memory_agent.store import ingest_episode
from memory_agent.worker import run_once
from tests.conftest import at
from tests.fakes import echo_answerer, gold_extractor, rule_updater
from tests.helpers import F, facts, job, say, space

G = {
    "TapOn 二面下周二下午": [F("TapOn 二面", "时间", "2026-10-27 下午", "TapOn 二面在 2026-10-27（周二）下午")],
    "二面改到周四了": [F("TapOn 二面", "时间", "2026-10-29 下午", "TapOn 二面改到 2026-10-29（周四）下午")],
    "我的期望薪资是 30k": [F("我", "期望薪资", "30k", "用户的期望薪资是 30k")],
}


@pytest.fixture
def models():
    with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
        yield


def _count(engine, sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def test_cascade_delete_all_versions_and_verify(engine, owner, models):
    s = space(engine, owner)
    e1 = say(engine, s, "TapOn 二面下周二下午", at(23))["episode_id"]
    e2 = say(engine, s, "二面改到周四了", at(24))["episode_id"]
    say(engine, s, "我的期望薪资是 30k", at(24))
    old, new, salary = facts(engine, s)
    out = delete_memory(engine, s, new["id"])
    assert out["result"] == "verified" and out["verification"]["total_hits"] == 0
    assert out["scope"]["fact_ids"] == [old["id"], new["id"]] and out["scope"]["episode_ids"] == [e1, e2]
    assert [f["id"] for f in facts(engine, s)] == [salary["id"]]
    assert _count(engine, "SELECT count(*) FROM retrieval_keys WHERE owner_type='fact' AND owner_id = ANY(:i)", i=[old["id"], new["id"]]) == 0
    assert _count(engine, "SELECT count(*) FROM retrieval_keys WHERE owner_type='episode_round' AND owner_id = ANY(:i)", i=[e1, e2]) == 0
    assert _count(engine, "SELECT count(*) FROM episodes WHERE id = ANY(:i) AND excluded AND content IS NULL", i=[e1, e2]) == 2
    # deletion_log：结果写入，只存哈希
    with engine.connect() as c:
        log = c.execute(text("SELECT result, verification, content_hashes, scope FROM deletion_log WHERE id=:i"), {"i": out["deletion_log_id"]}).mappings().one()
        ops_text = " ".join(str(r[0]) + str(r[1]) for r in c.execute(text("SELECT before, after FROM memory_ops WHERE space_id=:s"), {"s": s}))
    assert log["result"] == "verified" and log["verification"]["total_hits"] == 0 and len(log["content_hashes"]) == 4
    assert "TapOn" not in str(log["scope"]) and "2026-10-29" not in ops_text  # 审计里的正文已擦除
    # 删除后不复现
    with engine.begin() as c:
        r = recall(c, s, "TapOn 二面什么时候？")
    assert all("TapOn" not in m["fact_text"] for m in r["memories"])
    with engine.begin() as c:
        r2 = recall(c, s, "我期望薪资多少")
    assert "30k" in r2["answer"]  # 别的记忆不受影响


def test_verification_detects_leftover(engine, owner, models):
    """负例：如果有残留的近重复内容，验证必须报 failed（证明验证不是空转）。"""
    s = space(engine, owner)
    say(engine, s, "TapOn 二面下周二下午", at(23))
    (f,) = facts(engine, s)
    # 人为制造一条另一版本链里的同文副本（模拟“被后台整理学回来”）
    with engine.begin() as c:
        ep = ingest_episode(c, space_id=s, session_id="other", content="别处的一句话", occurred_at=at(23))
        c.execute(text("UPDATE jobs SET status='cancelled' WHERE id=:i"), {"i": ep["job_id"]})
        c.execute(text("""INSERT INTO facts(space_id, lineage_id, subject, predicate, object_text, fact_text, fact_key, valid_from, status,
                          source_episode_ids, origin_episode_id, dedupe_hash, created_by)
                          VALUES (:s, 0, 'TapOn 二面', '时间', '2026-10-27 下午', :t, 'x', now(), 'active', ARRAY[CAST(:e AS bigint)], :e, 'dup', 'consolidation')"""),
                  {"s": s, "t": f["fact_text"], "e": ep["episode_id"]})
    out = delete_memory(engine, s, f["id"])
    assert out["result"] == "failed" and out["verification"]["keyword_hits"] > 0


def test_pending_job_of_deleted_episode_is_skipped(engine, owner, models):
    s = space(engine, owner)
    say(engine, s, "TapOn 二面下周二下午", at(23))
    (f,) = facts(engine, s)
    with engine.begin() as c:  # 同一轮被重新排了作业（例如重放）
        c.execute(text("UPDATE jobs SET status='done' WHERE idempotency_key=:k"), {"k": f"extract:{f['origin_episode_id']}"})
        c.execute(text("INSERT INTO jobs(kind, payload, idempotency_key, status) VALUES ('extract', jsonb_build_object('episode_id', CAST(:e AS bigint)), :k, 'running')"),
                  {"e": f["origin_episode_id"], "k": f"replay:{f['origin_episode_id']}"})
        jid = c.execute(text("SELECT id FROM jobs WHERE idempotency_key=:k"), {"k": f"replay:{f['origin_episode_id']}"}).scalar_one()
        c.execute(text("UPDATE jobs SET status='queued' WHERE id=:i"), {"i": jid})
    delete_memory(engine, s, f["id"])
    assert job(engine, jid)["status"] == "cancelled"
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET status='queued' WHERE id=:i"), {"i": jid})
    run_once(engine)
    assert job(engine, jid)["result"] == {"skipped": "episode excluded or missing"} and facts(engine, s) == []


def test_api_delete_and_cross_owner_404(client, engine, owner, models):
    s = space(engine, owner)
    say(engine, s, "TapOn 二面下周二下午", at(23))
    (f,) = facts(engine, s)
    assert client.delete(f"/memories/{f['id']}", headers={"X-Owner-Id": "someone-else"}).status_code == 404
    lst = client.get("/memories?space=work", headers={"X-Owner-Id": owner}).json()["memories"]
    assert lst[0]["current"]["sources"][0]["excerpt"] == "TapOn 二面下周二下午"
    r = client.delete(f"/memories/{f['id']}", headers={"X-Owner-Id": owner}).json()
    assert r["result"] == "verified"
    assert client.get("/memories?space=work", headers={"X-Owner-Id": owner}).json()["memories"] == []


def test_sibling_facts_from_same_episode_are_reported(engine, owner):
    """一句话拆成两条记忆：删一条，另一条保留，并在 scope.sibling_facts_kept 里如实报告。"""
    g = {"TapOn 今天约了二面，下周二下午": [
        F("TapOn 二面", "时间", "2026-10-27 下午", "TapOn 二面在 2026-10-27（周二）下午"),
        F("TapOn 二面", "状态", "已约", "用户已约好 TapOn 二面"),
    ]}
    with llm.use_models(extract=gold_extractor(g), update=rule_updater(), answer=echo_answerer()):
        s = space(engine, owner)
        say(engine, s, "TapOn 今天约了二面，下周二下午", at(23))
        t, st = facts(engine, s)
        out = delete_memory(engine, s, t["id"])
    assert out["result"] == "verified"
    assert out["scope"]["sibling_facts_kept"] == [st["id"]]
    assert [f["id"] for f in facts(engine, s)] == [st["id"]]
