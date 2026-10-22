from __future__ import annotations

from sqlalchemy import text

from memory_agent.store import get_space_id, ingest_episode
from memory_agent.worker import run_until_idle


def space(engine, owner, name="work") -> int:
    with engine.begin() as c:
        return get_space_id(c, owner, name)


def say(engine, space_id, content, when, session="s1", **kw) -> dict:
    with engine.begin() as c:
        r = ingest_episode(c, space_id=space_id, session_id=session, content=content, occurred_at=when, **kw)
    run_until_idle(engine)
    return r


def facts(engine, space_id, status=None) -> list[dict]:
    q = "SELECT * FROM facts WHERE space_id=:s" + (" AND status=:st" if status else "") + " ORDER BY id"
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(q), {"s": space_id, "st": status}).mappings()]


def job(engine, job_id) -> dict:
    with engine.connect() as c:
        return dict(c.execute(text("SELECT * FROM jobs WHERE id=:i"), {"i": job_id}).mappings().one())


def F(subject, predicate, obj, fact_text, **kw) -> dict:
    return {"subject": subject, "predicate": predicate, "object_text": obj, "fact_text": fact_text, **kw}
