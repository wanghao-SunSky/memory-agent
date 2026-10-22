"""检索 + 回答：向量检索、space 过滤、只取 active；结构化回答 answer/used_memory_ids/abstain。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta

from pydantic_ai import Agent
from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import config, llm, prompts
from .embedding import embed_query
from .schemas import RecallAnswer
from .tracing import current_trace_id, tracer

answer_agent: Agent[None, RecallAnswer] = Agent(
    None, output_type=RecallAnswer, instructions=prompts.ANSWER_SYSTEM, name="answerer", retries=2, defer_model_check=True
)
ABSTAIN_TEXT = "没有相关记忆"
TZ = timezone(timedelta(hours=8))


def _d(v):
    return v.astimezone(TZ).date().isoformat() if v else None


def retrieve(conn: Connection, space_id: int, question: str, k: int | None = None, max_distance: float | None = None) -> list[dict]:
    k = k or config.RECALL_TOP_K
    max_distance = config.RECALL_MAX_DISTANCE if max_distance is None else max_distance
    q = embed_query(question)
    with tracer().start_as_current_span("retrieval.vector") as span:
        rows = conn.execute(
            text("""SELECT f.id, f.lineage_id, f.fact_text, f.valid_from, f.valid_to, f.source_episode_ids, f.supersedes_id,
                           rk.embedding <=> :q AS dist
                    FROM retrieval_keys rk JOIN facts f ON rk.owner_type='fact' AND rk.owner_id=f.id
                    WHERE rk.space_id=:s AND f.space_id=:s AND f.status='active'
                    ORDER BY dist LIMIT :k"""),
            {"q": q, "s": space_id, "k": k},
        ).mappings().all()
        hits = [dict(r) for r in rows if r["dist"] <= max_distance]
        span.set_attribute("retrieval.k", k)
        span.set_attribute("retrieval.raw_hits", len(rows))
        span.set_attribute("retrieval.hits", len(hits))
        if rows:
            span.set_attribute("retrieval.best_distance", float(rows[0]["dist"]))
    return hits


def _history(conn: Connection, space_id: int, lineage_id: int, current_id: int) -> list[dict]:
    rows = conn.execute(
        text("""SELECT id, fact_text, status, valid_from, valid_to, expired_at FROM facts
                WHERE space_id=:s AND lineage_id=:l AND id<>:c ORDER BY recorded_at, id"""),
        {"s": space_id, "l": lineage_id, "c": current_id},
    ).mappings().all()
    return [{"memory_id": r["id"], "fact_text": r["fact_text"], "status": r["status"], "valid_from": _d(r["valid_from"]),
             "valid_to": _d(r["valid_to"]), "expired_at": r["expired_at"].isoformat() if r["expired_at"] else None} for r in rows]


def recall(conn: Connection, space_id: int, question: str, as_of: datetime | None = None) -> dict:
    t0 = time.perf_counter()
    as_of = as_of or datetime.now(TZ)
    with tracer().start_as_current_span("recall") as span:
        trace_id = current_trace_id()
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("memory.space_id", space_id)
        span.set_attribute("memory.question_length", len(question))
        t_r = time.perf_counter()
        hits = retrieve(conn, space_id, question)
        retrieval_ms = (time.perf_counter() - t_r) * 1000
        memories = []
        for h in hits:
            eps = conn.execute(
                text("SELECT id, session_id, turn_idx, occurred_at FROM episodes WHERE id = ANY(:ids) AND excluded=false"),
                {"ids": h["source_episode_ids"]},
            ).mappings().all()
            memories.append({
                "memory_id": h["id"], "fact_text": h["fact_text"], "valid_from": _d(h["valid_from"]), "valid_to": _d(h["valid_to"]),
                "distance": round(float(h["dist"]), 4),
                "sources": [{"episode_id": e["id"], "session_id": e["session_id"], "turn_idx": e["turn_idx"],
                             "occurred_at": e["occurred_at"].isoformat()} for e in eps],
                "history": _history(conn, space_id, h["lineage_id"], h["id"]),
            })
        usage = {"input_tokens": 0, "output_tokens": 0}
        if not memories:
            # 没有任何相关记忆：直接弃答，不调模型
            out = RecallAnswer(answer=ABSTAIN_TEXT, used_memory_ids=[], abstain=True)
            span.set_attribute("recall.short_circuit", True)
        else:
            payload = {
                "question": question,
                "today": as_of.astimezone(TZ).date().isoformat(),
                "memories": [{k: m[k] for k in ("memory_id", "fact_text", "valid_from", "valid_to")} |
                             {"history": [{"fact_text": x["fact_text"], "status": "已作废", "valid_from": x["valid_from"], "valid_to": x["valid_to"]}
                                          for x in m["history"]]} for m in memories],
            }
            res = answer_agent.run_sync(json.dumps(payload, ensure_ascii=False), model=llm.get_model("answer"), model_settings=llm.MODEL_SETTINGS)
            out = res.output
            u = res.usage() if callable(res.usage) else res.usage
            usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0}
            valid = {m["memory_id"] for m in memories}
            used = [i for i in out.used_memory_ids if i in valid]
            if out.abstain:
                out = RecallAnswer(answer=out.answer or ABSTAIN_TEXT, used_memory_ids=[], abstain=True)
            else:
                out = out.model_copy(update={"used_memory_ids": used})
        span.set_attribute("recall.abstain", out.abstain)
        span.set_attribute("recall.used_memory_count", len(out.used_memory_ids))
        span.set_attribute("gen_ai.usage.input_tokens", usage["input_tokens"])
        span.set_attribute("gen_ai.usage.output_tokens", usage["output_tokens"])
        span.set_attribute("prompt_version", prompts.ANSWER_VERSION)
    used = set(out.used_memory_ids)
    return {
        "answer": out.answer, "abstain": out.abstain, "used_memory_ids": out.used_memory_ids,
        "memories": [m | {"used": m["memory_id"] in used} for m in memories],
        "trace_id": trace_id, "usage": usage,
        "latency_ms": {"retrieval": round(retrieval_ms, 1), "total": round((time.perf_counter() - t0) * 1000, 1)},
    }
