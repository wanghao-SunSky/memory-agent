"""20 条回归用例，两组：offline（金标抽取 + 规则更新 + 回显回答，确定性）与 live（真实 DeepSeek）。"""
from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from memory_agent import llm
from memory_agent.deletion import delete_memory
from memory_agent.recall import recall
from memory_agent.store import get_space_id, ingest_episode
from memory_agent.worker import run_until_idle
from tests.fakes import echo_answerer, gold_extractor, rule_updater
from tests.regression.cases import CASES, Case

TZ = timezone(timedelta(hours=8))


def _t(day, hour=10):
    return datetime(2026, 10, day, hour, 0, tzinfo=TZ) if day <= 31 else datetime(2026, 11, day - 31, hour, tzinfo=TZ)


def run_case(engine, case: Case) -> dict:
    owner = f"reg-{case.id}-{uuid.uuid4().hex[:6]}"
    spaces = {}
    with engine.begin() as c:
        for n in ("work", "life"):
            spaces[n] = get_space_id(c, owner, n)
    for i, t in enumerate(case.turns):
        with engine.begin() as c:
            ingest_episode(c, space_id=spaces[t.space], session_id=f"{case.id}-{t.space}", content=t.content, occurred_at=_t(t.day, t.hour))
        run_until_idle(engine)
        with engine.connect() as c:  # 作业必须全部成功
            bad = c.execute(text("""SELECT count(*) FROM jobs j JOIN episodes e ON e.id = (j.payload->>'episode_id')::bigint
                                    WHERE e.space_id = ANY(:s) AND j.status <> 'done'"""), {"s": list(spaces.values())}).scalar_one()
        assert bad == 0, "有作业没有成功完成"
    deletions = []
    if case.delete_keyword:
        # 模拟用户在“你记得我什么”页面上，把含该关键词的每张记忆卡片都点删除（抽取器可能把一轮拆成多条记忆）
        while True:
            with engine.connect() as c:
                mid = c.execute(text("SELECT id FROM facts WHERE space_id=:s AND status='active' AND fact_text ILIKE :k ORDER BY id LIMIT 1"),
                                {"s": spaces[case.delete_space], "k": f"%{case.delete_keyword}%"}).scalar()
            if mid is None:
                break
            deletions.append(delete_memory(engine, spaces[case.delete_space], mid))
        assert deletions, f"没有找到含“{case.delete_keyword}”的记忆可删"
    with engine.begin() as c:
        r = recall(c, spaces[case.q_space], case.question, as_of=_t(case.q_day, 17))
    return {"owner": owner, "spaces": spaces, "recall": r, "deletions": deletions}


def check(engine, case: Case, out: dict) -> None:
    e, r = case.expect, out["recall"]
    ans = r["answer"]
    ctx = f"[{case.id} {case.title}] answer={ans!r} abstain={r['abstain']}"
    if e.abstain is not None:
        assert r["abstain"] is e.abstain, ctx
    for group in e.contains_any:
        assert any(g in ans for g in group), f"{ctx} 应包含 {group} 之一"
    for bad in e.not_contains:
        assert bad not in ans, f"{ctx} 不应包含 {bad}"
        assert all(bad not in m["fact_text"] for m in r["memories"]), f"{ctx} 检索结果不应包含 {bad}"
    if e.history_contains_any:
        hist = " ".join(h["fact_text"] for m in r["memories"] for h in m["history"])
        assert any(x in hist for x in e.history_contains_any), f"{ctx} 已作废历史里应有 {e.history_contains_any}，实际 {hist!r}"
    sids = list(out["spaces"].values())
    with engine.connect() as c:
        for bad in e.no_memory_contains:
            n = c.execute(text("SELECT count(*) FROM facts WHERE space_id = ANY(:s) AND fact_text || object_text LIKE :k"), {"s": sids, "k": f"%{bad}%"}).scalar_one()
            n += c.execute(text("SELECT count(*) FROM episodes WHERE space_id = ANY(:s) AND content LIKE :k"), {"s": sids, "k": f"%{bad}%"}).scalar_one()
            assert n == 0, f"{ctx} 敏感串 {bad} 出现在记忆/原文中"
        if e.active_count:
            kw, n = e.active_count
            got = c.execute(text("SELECT count(*) FROM facts WHERE space_id = ANY(:s) AND status='active' AND fact_text LIKE :k"), {"s": sids, "k": f"%{kw}%"}).scalar_one()
            assert got == n, f"{ctx} 含“{kw}”的有效事实应为 {n} 条，实际 {got}"
    if e.deletion_verified:
        for d in out["deletions"]:
            assert d["result"] == "verified" and d["verification"]["total_hits"] == 0, f"{ctx} 删除验证 {d['verification']}"


def _gold() -> dict:
    g = {}
    for c in CASES:
        for t in c.turns:
            g.setdefault(t.content, t.gold)
    # 金标以打码后的原文为键
    from memory_agent.redact import redact
    return {redact(k)[0]: v for k, v in g.items()}


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_offline(engine, case):
    with llm.use_models(extract=gold_extractor(_gold()), update=rule_updater(), answer=echo_answerer()):
        check(engine, case, run_case(engine, case))


@pytest.mark.live
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_live(engine, case, record_property):
    out = run_case(engine, case)
    record_property("answer", out["recall"]["answer"])
    print(f"\n[{case.id}] deletes={len(out['deletions'])} Q={case.question} → {out['recall']['answer']} (abstain={out['recall']['abstain']}, total={out['recall']['latency_ms']['total']}ms)")
    check(engine, case, out)
