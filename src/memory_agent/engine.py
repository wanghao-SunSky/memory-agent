"""冷路径：抽取器 + 更新器。模型只返回结构化操作；代码在一个事务里执行并写 memory_ops。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, time as dtime, timezone, timedelta

from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from . import config, jobs, llm, prompts
from .embedding import embed_passage, embed_passages
from .schemas import CandidateFact, ExtractionResult, UpdateDecision
from .timeutil import reference_calendar
from .tracing import current_trace_id, tracer

extract_agent: Agent[None, ExtractionResult] = Agent(
    None, output_type=ExtractionResult, instructions=prompts.EXTRACT_SYSTEM, name="extractor", retries=2, defer_model_check=True
)
update_agent: Agent[None, UpdateDecision] = Agent(
    None, output_type=UpdateDecision, instructions=prompts.UPDATE_SYSTEM, name="updater", retries=2, defer_model_check=True
)

RECENT_M = 10


class ConcurrentUpdate(Exception):
    """乐观锁冲突：作业整体回滚并按退避重试（即“重试整个决策”）。"""


class EpisodeExcluded(Exception):
    pass


_PUNCT = re.compile(r"[\s\u3000,，。.!！?？:：;；'\"“”‘’()（）\[\]【】<>《》、\-_/]+")


def normalize(s: str) -> str:
    return _PUNCT.sub("", s).lower()


def fact_key(subject: str, predicate: str) -> str:
    return f"{normalize(subject)}|{normalize(predicate)}"


def _usage(result) -> dict:
    u = result.usage() if callable(result.usage) else result.usage
    return {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0}


def _active_fact_rows(conn: Connection, space_id: int, ids: list[int] | None = None) -> list[dict]:
    q = """SELECT id, subject, predicate, object_text, fact_text, fact_key, valid_from, valid_to, version
           FROM facts WHERE space_id=:s AND status='active'"""
    params: dict = {"s": space_id}
    if ids is not None:
        q += " AND id = ANY(:ids)"
        params["ids"] = ids
    return [dict(r) for r in conn.execute(text(q), params).mappings()]


def similar_active_facts(conn: Connection, space_id: int, query_vec, k: int, max_distance: float) -> list[tuple[int, float]]:
    rows = conn.execute(
        text("""SELECT f.id, rk.embedding <=> :q AS dist
                FROM retrieval_keys rk JOIN facts f ON rk.owner_type='fact' AND rk.owner_id=f.id
                WHERE rk.space_id=:s AND f.space_id=:s AND f.status='active'
                ORDER BY dist LIMIT :k"""),
        {"q": query_vec, "s": space_id, "k": k},
    ).all()
    return [(r[0], float(r[1])) for r in rows if r[1] <= max_distance]


def _fact_brief(r: dict) -> dict:
    return {"id": r["id"], "subject": r["subject"], "predicate": r["predicate"], "object_text": r["object_text"],
            "fact_text": r["fact_text"], "valid_from": r["valid_from"].date().isoformat() if r.get("valid_from") else None}


# ---------------- 抽取 ----------------

def build_extract_input(conn: Connection, ep: dict) -> dict:
    recent = conn.execute(
        text("""SELECT id, role, content, occurred_at FROM episodes
                WHERE space_id=:s AND session_id=:sid AND id <> :id AND excluded=false AND occurred_at <= :oa
                ORDER BY occurred_at DESC, id DESC LIMIT :m"""),
        {"s": ep["space_id"], "sid": ep["session_id"], "id": ep["id"], "oa": ep["occurred_at"], "m": RECENT_M},
    ).mappings().all()
    vec = embed_passage(ep["content"])
    sim_ids = [i for i, _ in similar_active_facts(conn, ep["space_id"], vec, 15, 1.0)]
    existing = _active_fact_rows(conn, ep["space_id"], sim_ids) if sim_ids else []
    return {
        "参考日历": reference_calendar(ep["occurred_at"].astimezone(timezone(timedelta(hours=8)))),
        "existing_facts": [_fact_brief(r) for r in existing],
        "recent_turns": [{"episode_id": r["id"], "role": r["role"], "occurred_at": r["occurred_at"].isoformat(), "content": r["content"]}
                         for r in reversed(recent)],
        "current_turn": {"episode_id": ep["id"], "occurred_at": ep["occurred_at"].isoformat(), "content": ep["content"]},
    }


def extract(conn: Connection, ep: dict) -> tuple[list[CandidateFact], dict]:
    payload = build_extract_input(conn, ep)
    with tracer().start_as_current_span("extract") as span:
        span.set_attribute("memory.episode_id", ep["id"])
        span.set_attribute("memory.content_length", len(ep["content"] or ""))
        span.set_attribute("memory.prompt_version", prompts.EXTRACT_VERSION)
        res = extract_agent.run_sync(json.dumps(payload, ensure_ascii=False), model=llm.get_model("extract"), model_settings=llm.MODEL_SETTINGS)
        usage = _usage(res)
        allowed = {ep["id"]} | {t["episode_id"] for t in payload["recent_turns"]}
        facts = []
        for f in res.output.facts:
            ids = [i for i in f.source_episode_ids if i in allowed]
            if ep["id"] not in ids:  # 出处必须非空且包含本轮
                ids = [ep["id"], *ids]
            facts.append(f.model_copy(update={"source_episode_ids": sorted(set(ids))}))
        span.set_attribute("memory.candidates", len(facts))
        span.set_attribute("gen_ai.usage.input_tokens", usage["input_tokens"])
        span.set_attribute("gen_ai.usage.output_tokens", usage["output_tokens"])
    return facts, usage


# ---------------- 更新 ----------------

def decide(conn: Connection, space_id: int, cand: CandidateFact, cand_vec) -> tuple[UpdateDecision, list[dict], str, dict]:
    key = fact_key(cand.subject, cand.predicate)
    sim = similar_active_facts(conn, space_id, cand_vec, config.UPDATE_TOP_S, config.UPDATE_MAX_DISTANCE)
    ids = {i for i, _ in sim}
    ids |= {r[0] for r in conn.execute(text("SELECT id FROM facts WHERE space_id=:s AND status='active' AND fact_key=:k"), {"s": space_id, "k": key})}
    existing = _active_fact_rows(conn, space_id, sorted(ids)) if ids else []
    if not existing:
        return UpdateDecision(op="ADD", target_fact_id=None, reason="无相似的已有事实"), [], "rule:no-candidates", {"input_tokens": 0, "output_tokens": 0}
    payload = {"candidate": cand.model_dump(mode="json", exclude={"source_episode_ids", "confidence"}),
               "existing": [_fact_brief(r) for r in existing]}
    with tracer().start_as_current_span("update.decide") as span:
        span.set_attribute("memory.existing_count", len(existing))
        res = update_agent.run_sync(json.dumps(payload, ensure_ascii=False), model=llm.get_model("update"), model_settings=llm.MODEL_SETTINGS)
        d = res.output
        valid = {r["id"] for r in existing}
        if d.op in ("INVALIDATE", "NOOP") and d.target_fact_id not in valid:
            d = UpdateDecision(op="ADD", target_fact_id=None, reason=f"模型返回了无效 target_fact_id，按 ADD 处理（原: {d.op}）")
        span.set_attribute("memory.op", d.op)
        return d, existing, llm.model_name("update"), _usage(res)


def _insert_fact(conn: Connection, space_id: int, ep: dict, cand: CandidateFact, valid_from: datetime,
                 lineage_id: int | None, supersedes_id: int | None, vec) -> int | None:
    key = fact_key(cand.subject, cand.predicate)
    dh = hashlib.sha256(f"{key}|{normalize(cand.object_text)}".encode()).hexdigest()
    row = conn.execute(
        text("""INSERT INTO facts(space_id, lineage_id, subject, predicate, object_text, fact_text, fact_key, time_expression,
                                  valid_from, status, confidence, source_episode_ids, origin_episode_id, dedupe_hash,
                                  supersedes_id, created_by, last_confirmed_at)
                VALUES (:s, 0, :sub, :pred, :obj, :ft, :k, :te, :vf, 'active', :c, :src, :oe, :dh, :sup, 'worker', :vf)
                ON CONFLICT (space_id, origin_episode_id, dedupe_hash) DO NOTHING RETURNING id"""),
        {"s": space_id, "sub": cand.subject, "pred": cand.predicate, "obj": cand.object_text, "ft": cand.fact_text, "k": key,
         "te": cand.time_expression, "vf": valid_from, "c": cand.confidence, "src": cand.source_episode_ids,
         "oe": ep["id"], "dh": dh, "sup": supersedes_id},
    ).first()
    if row is None:
        return None  # 幂等：同一轮重复抽出同一事实
    fid = row[0]
    conn.execute(text("UPDATE facts SET lineage_id=:l WHERE id=:i"), {"l": lineage_id or fid, "i": fid})
    conn.execute(
        text("INSERT INTO retrieval_keys(space_id, owner_type, owner_id, key_text, embedding) VALUES (:s,'fact',:i,:t,:e)"),
        {"s": space_id, "i": fid, "t": cand.fact_text, "e": vec},
    )
    return fid


def _op_log(conn, space_id, op, fact_id, episode_id, before, after, reason, model, prompt_version, trace_id):
    conn.execute(
        text("""INSERT INTO memory_ops(space_id, op, fact_id, episode_id, before, after, reason, model, prompt_version, trace_id, actor)
                VALUES (:s,:op,:f,:e,CAST(:b AS jsonb),CAST(:a AS jsonb),:r,:m,:pv,:t,'system')"""),
        {"s": space_id, "op": op, "f": fact_id, "e": episode_id, "b": json.dumps(before, ensure_ascii=False, default=str) if before else None,
         "a": json.dumps(after, ensure_ascii=False, default=str) if after else None, "r": reason, "m": model, "pv": prompt_version, "t": trace_id},
    )


def apply_candidate(conn: Connection, ep: dict, cand: CandidateFact, trace_id: str | None) -> dict:
    space_id = ep["space_id"]
    vec = embed_passage(cand.fact_text)
    decision, existing, model, usage = decide(conn, space_id, cand, vec)
    if cand.valid_from:
        valid_from = datetime.combine(cand.valid_from, dtime.min, tzinfo=ep["occurred_at"].tzinfo)
    else:
        valid_from = ep["occurred_at"]
    pv = prompts.UPDATE_VERSION if not model.startswith("rule:") else None
    after = cand.model_dump(mode="json")

    if decision.op == "ADD":
        fid = _insert_fact(conn, space_id, ep, cand, valid_from, None, None, vec)
        if fid:
            _op_log(conn, space_id, "ADD", fid, ep["id"], None, after, decision.reason, model, pv, trace_id)
        return {"op": "ADD", "fact_id": fid, "usage": usage}

    target = conn.execute(
        text("SELECT id, lineage_id, version, valid_from, fact_text, object_text, status FROM facts WHERE id=:i FOR UPDATE"),
        {"i": decision.target_fact_id},
    ).mappings().one()
    # 乐观锁：与“模型做决策时看到的版本”比较；不一致说明决策依据已过期，整个作业回滚重试
    seen_version = next(r["version"] for r in existing if r["id"] == target["id"])
    if target["status"] != "active" or target["version"] != seen_version:
        raise ConcurrentUpdate(f"fact {target['id']} 在决策期间被修改")
    if decision.op == "NOOP":
        n = conn.execute(
            text("UPDATE facts SET last_confirmed_at=GREATEST(COALESCE(last_confirmed_at, :t), :t), version=version+1 WHERE id=:i AND version=:v"),
            {"t": ep["occurred_at"], "i": target["id"], "v": target["version"]},
        ).rowcount
        if n != 1:
            raise ConcurrentUpdate(f"fact {target['id']} version 冲突")
        conn.execute(text("UPDATE facts SET source_episode_ids = array_append(source_episode_ids, CAST(:e AS bigint)) WHERE id=:i AND NOT (CAST(:e AS bigint) = ANY(source_episode_ids))"), {"e": ep["id"], "i": target["id"]})
        _op_log(conn, space_id, "NOOP", target["id"], ep["id"], None, {"confirmed_at": ep["occurred_at"]}, decision.reason, model, pv, trace_id)
        return {"op": "NOOP", "fact_id": target["id"], "usage": usage}

    # INVALIDATE：旧事实 valid_to = 新事实 valid_from，status=superseded，expired_at=now；插入新版本
    vf = max(valid_from, target["valid_from"])
    n = conn.execute(
        text("""UPDATE facts SET status='superseded', valid_to=:vt, expired_at=now(), version=version+1
                WHERE id=:i AND version=:v AND status='active'"""),
        {"vt": vf, "i": target["id"], "v": target["version"]},
    ).rowcount
    if n != 1:
        raise ConcurrentUpdate(f"fact {target['id']} version 冲突")
    fid = _insert_fact(conn, space_id, ep, cand, vf, target["lineage_id"], target["id"], vec)
    before = {"id": target["id"], "fact_text": target["fact_text"], "object_text": target["object_text"], "status": "active"}
    _op_log(conn, space_id, "INVALIDATE", target["id"], ep["id"], before, {"status": "superseded", "valid_to": vf, "superseded_by": fid},
            decision.reason, model, pv, trace_id)
    if fid:
        _op_log(conn, space_id, "ADD", fid, ep["id"], None, {**after, "supersedes_id": target["id"]}, "INVALIDATE 产生的新版本", model, pv, trace_id)
    return {"op": "INVALIDATE", "fact_id": fid, "superseded_id": target["id"], "usage": usage}


# ---------------- 作业执行 ----------------

def process_extract_job(engine: Engine, job: dict) -> dict:
    episode_id = int(job["payload"]["episode_id"])
    t0 = time.perf_counter()
    with tracer().start_as_current_span("job.extract") as span:
        trace_id = current_trace_id()
        span.set_attribute("job.id", job["id"])
        span.set_attribute("job.attempt", job["attempts"])
        span.set_attribute("memory.episode_id", episode_id)
        try:
            with engine.begin() as conn:
                # 锁定正在执行的作业：租约过期时其他 worker 会 SKIP LOCKED；
                # 重新领取过的旧执行者不得提交结果或覆盖新尝试。
                active = conn.execute(text("SELECT status, attempts, locked_by FROM jobs WHERE id=:i FOR UPDATE"),
                                      {"i": job["id"]}).mappings().first()
                if (active is None or active["status"] != "running" or active["attempts"] != job["attempts"]
                        or active["locked_by"] != job.get("locked_by")):
                    return {"skipped": "job lease no longer owned"}
                ep = conn.execute(
                    text("SELECT id, space_id, session_id, role, trust, content, occurred_at, excluded FROM episodes WHERE id=:i FOR SHARE"),
                    {"i": episode_id},
                ).mappings().first()
                if ep is None or ep["excluded"] or ep["content"] is None:
                    result = {"skipped": "episode excluded or missing"}
                    jobs.mark_done(conn, job["id"], result, trace_id)
                    span.set_attribute("job.result", "skipped")
                    return result
                ep = dict(ep)
                span.set_attribute("memory.space_id", ep["space_id"])
                cands, usage = extract(conn, ep)
                ops = []
                tokens = dict(usage)
                for c in cands:
                    r = apply_candidate(conn, ep, c, trace_id)
                    tokens["input_tokens"] += r["usage"]["input_tokens"]
                    tokens["output_tokens"] += r["usage"]["output_tokens"]
                    ops.append({k: v for k, v in r.items() if k != "usage"})
                # 原文轮次检索键（第 1 周 recall 只用事实键，原文键为第 2 周混合检索准备）
                conn.execute(
                    text("""INSERT INTO retrieval_keys(space_id, owner_type, owner_id, key_text, embedding)
                            VALUES (:s,'episode_round',:i,:t,:e) ON CONFLICT (owner_type, owner_id) DO NOTHING"""),
                    {"s": ep["space_id"], "i": ep["id"], "t": ep["content"], "e": embed_passages([ep["content"]])[0]},
                )
                result = {"ops": ops, "tokens": tokens, "latency_ms": round((time.perf_counter() - t0) * 1000)}
                jobs.mark_done(conn, job["id"], result, trace_id)
            for k, v in tokens.items():
                span.set_attribute(f"gen_ai.usage.{k}", v)
            span.set_attribute("memory.ops", ",".join(o["op"] for o in ops))
            span.set_attribute("job.result", "done")
            return result
        except Exception as e:
            # 异常消息可能含正文，trace 里只记类型
            span.set_status(Status(StatusCode.ERROR, type(e).__name__))
            span.set_attribute("error.type", type(e).__name__)
            status = jobs.mark_failed(engine, job, e, trace_id)
            span.set_attribute("job.result", status)
            raise
