"""用户删除（硬删除）：级联删除事实全部版本、检索键；来源轮次 excluded 且正文物理删除（留哈希）；
写 deletion_log；随后自动验证（关键词 + 向量检索命中必须为 0），结果写回 deletion_log。"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .embedding import embed_passage
from .engine import normalize
from .tracing import current_trace_id, tracer

# 向量“命中”的定义：与被删内容近乎重复（余弦距离 <= 该值）的任何残留检索键
NEAR_DUP_DISTANCE = 0.12


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def delete_memory(engine: Engine, space_id: int, memory_id: int) -> dict:
    with tracer().start_as_current_span("memory.delete") as span:
        span.set_attribute("memory.space_id", space_id)
        span.set_attribute("memory.memory_id", memory_id)
        with engine.begin() as conn:
            target = conn.execute(text("SELECT lineage_id FROM facts WHERE id=:i AND space_id=:s FOR UPDATE"), {"i": memory_id, "s": space_id}).first()
            if target is None:
                raise LookupError(memory_id)
            versions = conn.execute(
                text("SELECT id, subject, object_text, fact_text, source_episode_ids FROM facts WHERE space_id=:s AND lineage_id=:l FOR UPDATE"),
                {"s": space_id, "l": target[0]},
            ).mappings().all()
            fact_ids = [v["id"] for v in versions]
            episode_ids = sorted({e for v in versions for e in v["source_episode_ids"]})
            eps = conn.execute(text("SELECT id, content FROM episodes WHERE id = ANY(:ids) AND space_id=:s FOR UPDATE"),
                               {"ids": episode_ids, "s": space_id}).mappings().all()
            # 仅在内存里保留被删内容，用于事后验证；不落库
            probe = {
                "fact_texts": [v["fact_text"] for v in versions],
                "pairs": [(v["subject"], v["object_text"]) for v in versions],
                "episode_texts": [e["content"] for e in eps if e["content"]],
            }
            hashes = sorted({_sha(t) for t in probe["fact_texts"] + probe["episode_texts"]})

            n_fact_keys = conn.execute(text("DELETE FROM retrieval_keys WHERE owner_type='fact' AND owner_id = ANY(:ids)"), {"ids": fact_ids}).rowcount
            n_ep_keys = conn.execute(text("DELETE FROM retrieval_keys WHERE owner_type='episode_round' AND owner_id = ANY(:ids)"), {"ids": episode_ids}).rowcount
            conn.execute(text("UPDATE episodes SET excluded=true, excluded_at=now(), content=NULL WHERE id = ANY(:ids)"), {"ids": episode_ids})
            # 取消这些轮次还没跑的抽取作业，防止删除后被“重新学回来”
            conn.execute(text("""UPDATE jobs SET status='cancelled', updated_at=now()
                                 WHERE kind='extract' AND status IN ('queued') AND (payload->>'episode_id')::bigint = ANY(:ids)"""), {"ids": episode_ids})
            conn.execute(text("UPDATE facts SET supersedes_id=NULL WHERE supersedes_id = ANY(:ids)"), {"ids": fact_ids})
            n_facts = conn.execute(text("DELETE FROM facts WHERE id = ANY(:ids)"), {"ids": fact_ids}).rowcount
            # 审计行保留，但擦掉其中的正文
            conn.execute(text("""UPDATE memory_ops SET before=NULL, after=jsonb_build_object('scrubbed', true)
                                 WHERE space_id=:s AND (fact_id = ANY(:f) OR episode_id = ANY(:e))"""),
                         {"s": space_id, "f": fact_ids, "e": episode_ids})
            scope = {"fact_ids": fact_ids, "episode_ids": episode_ids, "deleted_facts": n_facts,
                     "deleted_fact_keys": n_fact_keys, "deleted_episode_keys": n_ep_keys, "episode_content": "physically_deleted"}
            conn.execute(text("""INSERT INTO memory_ops(space_id, op, fact_id, after, reason, actor, trace_id)
                                 VALUES (:s,'DELETE',:f,CAST(:a AS jsonb),'用户删除','user',:t)"""),
                         {"s": space_id, "f": memory_id, "a": json.dumps(scope), "t": current_trace_id()})
            log_id = conn.execute(
                text("""INSERT INTO deletion_log(space_id, target_memory_id, scope, content_hashes, result)
                        VALUES (:s,:m,CAST(:sc AS jsonb),:h,'pending') RETURNING id"""),
                {"s": space_id, "m": memory_id, "sc": json.dumps(scope), "h": hashes},
            ).scalar_one()
        verification = verify_deletion(engine, space_id, fact_ids, episode_ids, probe)
        result = "verified" if verification["total_hits"] == 0 else "failed"
        with engine.begin() as conn:
            conn.execute(text("UPDATE deletion_log SET result=:r, verification=CAST(:v AS jsonb), verified_at=now() WHERE id=:i"),
                         {"r": result, "v": json.dumps(verification), "i": log_id})
        span.set_attribute("deletion.result", result)
        span.set_attribute("deletion.facts", n_facts)
        return {"deletion_log_id": log_id, "scope": scope, "result": result, "verification": verification, "trace_id": current_trace_id()}


def verify_deletion(engine: Engine, space_id: int, fact_ids: list[int], episode_ids: list[int], probe: dict) -> dict:
    """命中数必须为 0。三类检查：残留行、关键词、向量（含真实 recall 检索路径）。"""
    from .recall import retrieve

    with tracer().start_as_current_span("memory.delete.verify"), engine.connect() as conn:
        residual = {
            "facts": conn.execute(text("SELECT count(*) FROM facts WHERE id = ANY(:i)"), {"i": fact_ids}).scalar_one(),
            "fact_keys": conn.execute(text("SELECT count(*) FROM retrieval_keys WHERE owner_type='fact' AND owner_id = ANY(:i)"), {"i": fact_ids}).scalar_one(),
            "episode_keys": conn.execute(text("SELECT count(*) FROM retrieval_keys WHERE owner_type='episode_round' AND owner_id = ANY(:i)"), {"i": episode_ids}).scalar_one(),
            "episode_content": conn.execute(text("SELECT count(*) FROM episodes WHERE id = ANY(:i) AND (content IS NOT NULL OR excluded=false)"), {"i": episode_ids}).scalar_one(),
        }
        kw_hits = 0
        for t in probe["fact_texts"] + probe["episode_texts"]:
            kw_hits += conn.execute(text("SELECT count(*) FROM facts WHERE space_id=:s AND fact_text = :t"), {"s": space_id, "t": t}).scalar_one()
            kw_hits += conn.execute(text("SELECT count(*) FROM retrieval_keys WHERE space_id=:s AND key_text = :t"), {"s": space_id, "t": t}).scalar_one()
            kw_hits += conn.execute(text("SELECT count(*) FROM episodes WHERE space_id=:s AND excluded=false AND content = :t"), {"s": space_id, "t": t}).scalar_one()
        for subj, obj in probe["pairs"]:
            if len(normalize(obj)) < 2:
                continue
            kw_hits += conn.execute(
                text("SELECT count(*) FROM facts WHERE space_id=:s AND strpos(fact_text, :a) > 0 AND strpos(fact_text, :b) > 0"),
                {"s": space_id, "a": subj, "b": obj},
            ).scalar_one()
        vec_hits = 0
        recall_hits = 0
        for t in probe["fact_texts"] + probe["episode_texts"]:
            v = embed_passage(t)
            vec_hits += conn.execute(
                text("SELECT count(*) FROM (SELECT embedding <=> :q AS d FROM retrieval_keys WHERE space_id=:s ORDER BY d LIMIT 20) x WHERE d <= :m"),
                {"q": v, "s": space_id, "m": NEAR_DUP_DISTANCE},
            ).scalar_one()
            # 走与 /recall 完全相同的检索函数：命中被删 id 或近重复内容都算
            for h in retrieve(conn, space_id, t, k=10, max_distance=1.0):
                if h["id"] in fact_ids or h["dist"] <= NEAR_DUP_DISTANCE:
                    recall_hits += 1
        total = sum(residual.values()) + kw_hits + vec_hits + recall_hits
        return {"residual": residual, "keyword_hits": kw_hits, "vector_hits": vec_hits, "recall_path_hits": recall_hits, "total_hits": total,
                "near_dup_distance": NEAR_DUP_DISTANCE, "probes": len(probe["fact_texts"]) + len(probe["episode_texts"])}
