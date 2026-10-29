"""固定样例比较单次检索回答与工具调查；只允许独立测试库，不清空现有表。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url

from memory_agent import config, llm
from memory_agent.db import get_engine
from memory_agent.deletion import delete_memory
from memory_agent.investigation import investigate
from memory_agent.migrate import migrate
from memory_agent.recall import recall
from memory_agent.store import get_space_id, ingest_episode
from memory_agent.worker import run_once

ROOT = Path(__file__).resolve().parents[1]


def score(case, result):
    """预先固定的字面评分，不假装是完整语义裁判。引用存在不等于语义正确。"""
    checks = {"abstention": result["abstain"] is case["abstain"],
              "citations": not result["used_memory_ids"] if result["abstain"] else bool(result["used_memory_ids"])}
    if case.get("contains"):
        checks["expected_content"] = case["contains"] in result["answer"]
    if case.get("forbidden"):
        checks["no_future_content"] = case["forbidden"] not in json.dumps(result, ensure_ascii=False)
    if "steps" in result:
        checks["searched"] = any(s["tool"] == "search_memories" for s in result["steps"])
    checks["finished"] = result.get("stop_reason", "completed") == "completed"
    return checks


def run_case(case, route, mode):
    engine = get_engine()
    owner = "eval-" + uuid.uuid4().hex
    with engine.begin() as conn:
        spaces = {name: get_space_id(conn, owner, name) for name in ("work", "life")}
    write_usage = {"input_tokens": 0, "output_tokens": 0}
    for i, turn in enumerate(case["turns"]):
        with engine.begin() as conn:
            queued = ingest_episode(conn, space_id=spaces[turn.get("space", "work")], session_id=owner,
                                    content=turn["content"], occurred_at=datetime.fromisoformat(turn["at"]), turn_idx=i)
        # Only process this evaluation's job; never consume unrelated queued work.
        run_once(engine, job_id=queued["job_id"])
        with engine.connect() as conn:
            job = conn.execute(text("SELECT status,result FROM jobs WHERE id=:id"), {"id": queued["job_id"]}).mappings().one()
        if job["status"] != "done":
            raise RuntimeError("extract_job_not_done")
        for key in write_usage:
            write_usage[key] += (job["result"] or {}).get("tokens", {}).get(key, 0)
    if case.get("delete"):
        while True:
            with engine.connect() as conn:
                memory_id = conn.execute(text("SELECT id FROM facts WHERE space_id=:s AND status='active' LIMIT 1"),
                                         {"s": spaces["work"]}).scalar()
            if memory_id is None:
                break
            if delete_memory(engine, spaces["work"], memory_id)["result"] != "verified":
                raise RuntimeError("deletion_not_verified")
    with engine.begin() as conn:
        result = {"recall": recall, "investigate": investigate}[route](
            conn, spaces[case.get("space", "work")], case["question"],
            as_of=datetime.fromisoformat(case["as_of"]) if case.get("as_of") else None)
    checks = score(case, result)
    return {"case": case["id"], "purpose": case["purpose"], "route": route, "mode": mode,
            "passed": all(checks.values()), "checks": checks, "answer": result["answer"],
            "abstain": result["abstain"], "used_memory_ids": result["used_memory_ids"],
            "query_usage": result["usage"], "write_usage": write_usage, "latency_ms": result["latency_ms"],
            "steps": result.get("steps", []), "stop_reason": result.get("stop_reason", "completed")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5:
        parser.error("repeats must be 1..5")
    if make_url(config.database_url()).database not in {"memory_test", "memory_test_learning"}:
        parser.error("评测仅允许 memory_test / memory_test_learning 独立数据库")
    os.environ["MEMORY_MODEL_MODE"] = args.mode
    os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = ""
    if args.mode == "live" and not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("live 评测需要 DEEPSEEK_API_KEY")
    migrate()
    cases = json.loads((ROOT / "evals/cases.json").read_text())
    results = []
    for repeat in range(args.repeats):
        for case in cases:
            for route in ("recall", "investigate"):
                start = time.perf_counter()
                try:
                    result = run_case(case, route, args.mode)
                except Exception as exc:
                    # Provider error strings can contain request bodies: only save class names.
                    result = {"case": case["id"], "route": route, "passed": False, "error_type": type(exc).__name__}
                result.update(repeat=repeat + 1, total_ms=round((time.perf_counter() - start) * 1000, 1))
                results.append(result)
                print(f"{case['id']} {route}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
    summary = {}
    for route in ("recall", "investigate"):
        rows = [r for r in results if r["route"] == route]
        query_times = [r["latency_ms"]["total"] for r in rows if "latency_ms" in r]
        answered_times = [r["latency_ms"]["total"] for r in rows if "latency_ms" in r and not r["abstain"]]
        abstained_times = [r["latency_ms"]["total"] for r in rows if "latency_ms" in r and r["abstain"]]
        summary[route] = {"passed": sum(r["passed"] for r in rows), "total": len(rows),
                          "query_median_ms": statistics.median(query_times) if query_times else None,
                          "answered_query_median_ms": statistics.median(answered_times) if answered_times else None,
                          "abstained_query_median_ms": statistics.median(abstained_times) if abstained_times else None,
                          "query_tokens": {key: sum(r.get("query_usage", {}).get(key, 0) for r in rows)
                                           for key in ("input_tokens", "output_tokens")}}
    report = {"run_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode,
              "model": llm.model_name("answer"), "repeats": args.repeats, "dataset": "evals/cases.json",
              "limits": "8 synthetic cases; literal checks, not general model accuracy. Demo is rules, not an LLM. Query timings exclude writes. No dollar cost estimate without provider billing rates.",
              "summary": summary, "results": results}
    output = args.output or ROOT / f"reports/evaluation-{args.mode}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
