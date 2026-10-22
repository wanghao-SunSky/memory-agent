"""空间、写入（热路径）、记忆列表。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .redact import redact

SPACE_NAMES = {"work": "工作", "life": "生活"}
SOURCE_TRUST = {"user_utterance": "trusted"}  # 其他来源一律 untrusted


class NotFound(Exception):
    pass


def get_space_id(conn: Connection, owner_id: str, name: str) -> int:
    if name not in SPACE_NAMES:
        raise ValueError(f"space 只能是 {list(SPACE_NAMES)}，收到 {name!r}")
    row = conn.execute(
        text("""INSERT INTO spaces(owner_id, name, display_name) VALUES (:o,:n,:d)
                ON CONFLICT (owner_id, name) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id"""),
        {"o": owner_id, "n": name, "d": SPACE_NAMES[name]},
    ).one()
    return row[0]


def content_hash(space_id: int, session_id: str, role: str, occurred_at: datetime, content: str) -> str:
    raw = json.dumps([space_id, session_id, role, occurred_at.astimezone(timezone.utc).isoformat(), content.strip()], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def ingest_episode(
    conn: Connection,
    *,
    space_id: int,
    session_id: str,
    content: str,
    occurred_at: datetime,
    role: str = "user",
    source_type: str = "user_utterance",
    turn_idx: int | None = None,
) -> dict:
    """热路径：幂等写 episode + 同事务插入 extract 作业。不调模型。"""
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at 必须带时区")
    h = content_hash(space_id, session_id, role, occurred_at, content)
    existing = conn.execute(
        text("SELECT e.id, j.id FROM episodes e LEFT JOIN jobs j ON j.idempotency_key = 'extract:' || e.id WHERE e.space_id=:s AND e.content_hash=:h"),
        {"s": space_id, "h": h},
    ).first()
    if existing:
        return {"episode_id": existing[0], "job_id": existing[1], "duplicate": True, "redacted": None, "redaction_kinds": []}

    trust = SOURCE_TRUST.get(source_type, "untrusted")
    clean, kinds = redact(content)
    if turn_idx is None:
        turn_idx = conn.execute(
            text("SELECT COALESCE(MAX(turn_idx), -1) + 1 FROM episodes WHERE space_id=:s AND session_id=:sid"),
            {"s": space_id, "sid": session_id},
        ).scalar_one()
    row = conn.execute(
        text("""INSERT INTO episodes(space_id, session_id, turn_idx, role, source_type, trust, content, occurred_at, content_hash, redacted)
                VALUES (:s,:sid,:t,:r,:st,:tr,:c,:oa,:h,:red)
                ON CONFLICT (space_id, content_hash) DO NOTHING RETURNING id"""),
        {"s": space_id, "sid": session_id, "t": turn_idx, "r": role, "st": source_type, "tr": trust,
         "c": clean, "oa": occurred_at, "h": h, "red": bool(kinds)},
    ).first()
    if row is None:  # 并发下的重复提交
        eid = conn.execute(text("SELECT id FROM episodes WHERE space_id=:s AND content_hash=:h"), {"s": space_id, "h": h}).scalar_one()
        jid = conn.execute(text("SELECT id FROM jobs WHERE idempotency_key=:k"), {"k": f"extract:{eid}"}).scalar()
        return {"episode_id": eid, "job_id": jid, "duplicate": True, "redacted": None, "redaction_kinds": []}
    eid = row[0]
    if kinds:
        conn.execute(
            text("INSERT INTO memory_ops(space_id, op, episode_id, after, reason, actor) VALUES (:s,'REDACT',:e,CAST(:a AS jsonb),'敏感信息正则打码','system')"),
            {"s": space_id, "e": eid, "a": json.dumps({"kinds": kinds})},
        )
    job_id = None
    # 只有用户本人的发言进入抽取；untrusted 来源只保存原文（隔离抽取第 3 周做）
    if trust == "trusted" and role == "user":
        job_id = conn.execute(
            text("""INSERT INTO jobs(kind, payload, idempotency_key) VALUES ('extract', CAST(:p AS jsonb), :k)
                    ON CONFLICT (idempotency_key) DO UPDATE SET updated_at = now() RETURNING id"""),
            {"p": json.dumps({"episode_id": eid}), "k": f"extract:{eid}"},
        ).scalar_one()
    return {"episode_id": eid, "job_id": job_id, "duplicate": False, "redacted": bool(kinds), "redaction_kinds": kinds}


def _iso(v):
    return v.isoformat() if v is not None else None


def list_memories(conn: Connection, space_id: int, include_history: bool = True) -> list[dict]:
    """按版本链（lineage）分组列出；每个版本带出处和生效区间。"""
    rows = conn.execute(
        text("""SELECT id, lineage_id, subject, predicate, object_text, fact_text, valid_from, valid_to, recorded_at,
                       expired_at, last_confirmed_at, status, source_episode_ids, supersedes_id, version, confidence
                FROM facts WHERE space_id=:s ORDER BY lineage_id, recorded_at, id"""),
        {"s": space_id},
    ).mappings().all()
    ep_ids = sorted({e for r in rows for e in r["source_episode_ids"]})
    eps = {}
    if ep_ids:
        for e in conn.execute(
            text("SELECT id, session_id, turn_idx, occurred_at, content, excluded FROM episodes WHERE id = ANY(:ids)"), {"ids": ep_ids}
        ).mappings():
            eps[e["id"]] = e
    lineages: dict[int, list[dict]] = {}
    for r in rows:
        lineages.setdefault(r["lineage_id"], []).append({
            "memory_id": r["id"], "subject": r["subject"], "predicate": r["predicate"], "object_text": r["object_text"],
            "fact_text": r["fact_text"], "status": r["status"], "version": r["version"],
            "valid_from": _iso(r["valid_from"]), "valid_to": _iso(r["valid_to"]),
            "recorded_at": _iso(r["recorded_at"]), "expired_at": _iso(r["expired_at"]),
            "last_confirmed_at": _iso(r["last_confirmed_at"]), "supersedes_id": r["supersedes_id"],
            "sources": [
                {"episode_id": e, "session_id": eps[e]["session_id"], "turn_idx": eps[e]["turn_idx"],
                 "occurred_at": _iso(eps[e]["occurred_at"]), "excerpt": (eps[e]["content"] or "")[:80]}
                for e in r["source_episode_ids"] if e in eps
            ],
        })
    out = []
    for lid, versions in lineages.items():
        current = next((v for v in reversed(versions) if v["status"] == "active"), versions[-1])
        item = {"lineage_id": lid, "current": current}
        if include_history:
            item["versions"] = versions
        out.append(item)
    out.sort(key=lambda x: x["current"]["recorded_at"], reverse=True)
    return out


def owner_of_fact(conn: Connection, memory_id: int) -> tuple[int, str] | None:
    r = conn.execute(text("SELECT f.space_id, s.owner_id FROM facts f JOIN spaces s ON s.id=f.space_id WHERE f.id=:i"), {"i": memory_id}).first()
    return (r[0], r[1]) if r else None
