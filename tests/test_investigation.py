import asyncio
import json
import threading
import time

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text

from memory_agent import llm
from memory_agent.investigation import _read_tool, investigate
from tests.conftest import at
from tests.fakes import _call, gold_extractor, rule_updater
from tests.helpers import facts, say, space
from tests.test_recall import G


def tool_results(messages):
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def tool(name, args):
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])


def test_agent_really_searches_reads_history_and_sources(engine, owner):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
        say(engine, s, "二面改到周四了", at(24))
    seen = []
    def model(messages, info):
        rs = tool_results(messages)
        if not rs:
            return tool("search_memories", {"question": "TapOn 二面什么时候？"})
        found = rs[0].content
        mid = found[0]["memory_id"]
        if len(rs) == 1:
            return tool("get_memory_history", {"memory_id": mid})
        if len(rs) == 2:
            assert "2026-10-27" in json.dumps(rs[-1].content, ensure_ascii=False)
            return tool("get_source_evidence", {"memory_id": mid})
        assert "二面改到周四了" in json.dumps(rs[-1].content, ensure_ascii=False)
        seen.extend(p.tool_name for p in rs)
        return _call(info, {"answer": found[0]["fact_text"], "used_memory_ids": [mid], "abstain": False})
    with llm.use_models(investigate=FunctionModel(model)), engine.begin() as c:
        result = investigate(c, s, "面试是什么时候，之前改过吗？")
    assert seen == ["search_memories", "get_memory_history", "get_source_evidence"]
    assert [s["tool"] for s in result["steps"]] == seen
    assert result["usage"]["requests"] == 4
    assert not result["abstain"] and "2026-10-29" in result["answer"]
    assert "改到周四" not in json.dumps(result["steps"], ensure_ascii=False)


def test_tool_ids_are_scoped_and_guessed_citations_rejected(engine, owner):
    mine, other = space(engine, owner), space(engine, owner + "-other")
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, other, "TapOn 二面下周二下午", at(23))
    secret_id = facts(engine, other)[0]["id"]
    def model(messages, info):
        rs = tool_results(messages)
        if not rs:
            return tool("get_source_evidence", {"memory_id": secret_id})
        assert rs[-1].content == []
        if len(rs) == 1:
            return tool("get_memory_history", {"memory_id": secret_id})
        return _call(info, {"answer": "不应泄露的推测", "used_memory_ids": [secret_id], "abstain": False})
    with llm.use_models(investigate=FunctionModel(model)), engine.begin() as c:
        result = investigate(c, mine, "告诉我另一个人的记忆")
    assert result["abstain"] and result["used_memory_ids"] == []
    assert "推测" not in result["answer"] and result["memories"] == []
    assert all(step["status"] == "denied" for step in result["steps"])


@pytest.mark.parametrize("cite_old", [False, True])
def test_history_sources_require_disclosure_and_keep_citation_contract(engine, owner, cite_old):
    mine, other = space(engine, owner), space(engine, owner + "-other")
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, mine, "TapOn 二面下周二下午", at(23))
        say(engine, mine, "二面改到周四了", at(24))
        say(engine, other, "TapOn 二面下周二下午", at(23))
    with engine.connect() as c:
        old_id = c.scalar(text("SELECT id FROM facts WHERE space_id=:s AND status='superseded'"), {"s": mine})
    other_id = facts(engine, other)[0]["id"]

    def model(messages, info):
        rs = tool_results(messages)
        if not rs:
            return tool("search_memories", {"question": "TapOn 二面什么时候？"})
        current_id = rs[0].content[0]["memory_id"]
        if len(rs) == 1:
            return tool("get_source_evidence", {"memory_id": old_id})
        if len(rs) == 2:
            assert rs[-1].content == []  # 搜索尚未向模型披露旧版本编号。
            return tool("get_memory_history", {"memory_id": current_id})
        if len(rs) == 3:
            assert old_id in {r["memory_id"] for r in rs[-1].content}
            return tool("get_source_evidence", {"memory_id": old_id})
        if len(rs) == 4:
            evidence = json.dumps(rs[-1].content, ensure_ascii=False)
            assert "TapOn 二面下周二下午" in evidence
            assert "二面改到周四了" not in evidence
            return tool("get_source_evidence", {"memory_id": other_id})
        assert rs[-1].content == []
        return _call(info, {"answer": "原定周二，现为周四", "used_memory_ids": [old_id if cite_old else current_id], "abstain": False})

    with llm.use_models(investigate=FunctionModel(model)), engine.begin() as c:
        result = investigate(c, mine, "原来的安排和改期分别依据哪句话？")
    assert [step["status"] for step in result["steps"]] == ["ok", "denied", "ok", "ok", "denied"]
    assert result["abstain"] is cite_old
    assert old_id not in result["used_memory_ids"]


def test_default_query_time_is_frozen_for_all_tools(engine, owner, monkeypatch):
    seen = []

    def search(_engine, _deadline, _space, _question, as_of):
        seen.append(as_of)
        return []

    def model(messages, info):
        if len(tool_results(messages)) < 2:
            return tool("search_memories", {"question": "当前安排"})
        return _call(info, {"answer": "没有相关记忆", "used_memory_ids": [], "abstain": True})

    monkeypatch.setattr("memory_agent.investigation._search", search)
    with llm.use_models(investigate=FunctionModel(model)), engine.begin() as c:
        investigate(c, space(engine, owner), "当前安排")
    assert len(seen) == 2 and seen[0] is seen[1]
    assert seen[0].tzinfo is not None


def test_request_budget_stops_nonterminating_agent(engine, owner, monkeypatch):
    monkeypatch.setattr("memory_agent.investigation.MAX_REQUESTS", 2)
    model = FunctionModel(lambda m, i: tool("search_memories", {"question": "不存在的记忆"}))
    with llm.use_models(investigate=model), engine.begin() as c:
        result = investigate(c, space(engine, owner), "一直查下去")
    assert result["abstain"] and not result["used_memory_ids"]
    assert result["stop_reason"] == "budget_exceeded"
    assert result["usage"]["requests"] == 2


def test_tool_call_budget_rejects_oversized_batch(engine, owner, monkeypatch):
    monkeypatch.setattr("memory_agent.investigation.MAX_TOOL_CALLS", 2)
    def batch(messages, info):
        return ModelResponse(parts=[ToolCallPart("search_memories", {"question": "面试"}, tool_call_id=str(i)) for i in range(3)])
    with llm.use_models(investigate=FunctionModel(batch)), engine.begin() as c:
        result = investigate(c, space(engine, owner), "一次调用太多工具")
    assert result["abstain"] and result["stop_reason"] == "budget_exceeded"
    assert len(result["steps"]) <= 2


def test_timeout_returns_safe_abstention(engine, owner, monkeypatch):
    monkeypatch.setattr("memory_agent.investigation.TIMEOUT_SECONDS", 0.02)
    async def slow(messages, info):
        await asyncio.sleep(1)
        return _call(info, {"answer": "不应返回", "used_memory_ids": [], "abstain": False})
    with llm.use_models(investigate=FunctionModel(slow)), engine.begin() as c:
        result = investigate(c, space(engine, owner), "等待很久")
    assert result["abstain"] and result["stop_reason"] == "timeout"
    assert result["answer"] != "不应返回"


def test_blocked_sync_tool_times_out_without_reusing_outer_connection(engine, owner, monkeypatch):
    """模拟无法强杀的冷模型初始化：响应先结束，线程自行回收专属连接。"""
    monkeypatch.setattr("memory_agent.investigation.TIMEOUT_SECONDS", 0.1)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    tool_connections, errors = [], []

    def blocked_retrieve(conn, *args, **kwargs):
        tool_connections.append(conn)
        assert conn.scalar(text("SHOW transaction_read_only")) == "on"
        started.set()
        try:
            release.wait(3)
            # 过期后必须在发送 SQL 之前被截止时间检查拦住。
            conn.execute(text("SELECT 1"))
        except TimeoutError as exc:
            errors.append(exc)
            raise
        finally:
            finished.set()

    monkeypatch.setattr("memory_agent.investigation.retrieve", blocked_retrieve)
    model = FunctionModel(lambda m, i: tool("search_memories", {"question": "冷启动"}))
    s = space(engine, owner)
    try:
        with llm.use_models(investigate=model), engine.begin() as outer:
            start = time.monotonic()
            result = investigate(outer, s, "等待本地模型")
            assert started.is_set()
            assert time.monotonic() - start < 1
            assert result["stop_reason"] == "timeout" and result["abstain"]
            assert tool_connections[0] is not outer
            assert not finished.is_set()
        snapshot = json.dumps(result)
    finally:
        release.set()
        assert finished.wait(2)
    deadline = time.monotonic() + 2
    while not tool_connections[0].closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tool_connections[0].closed
    assert len(errors) == 1
    assert json.dumps(result) == snapshot
    assert result["steps"] == [] and result["memories"] == []


def test_tool_sql_uses_remaining_budget_and_releases_connection(engine):
    connections = []

    def slow_sql(conn):
        connections.append(conn)
        assert conn.scalar(text("SHOW transaction_read_only")) == "on"
        conn.execute(text("SELECT pg_sleep(2)"))

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        _read_tool(engine, time.monotonic() + 0.1, slow_sql)
    assert time.monotonic() - start < 1
    assert connections[0].closed


def test_expired_tool_does_not_call_reader(engine):
    with pytest.raises(TimeoutError):
        _read_tool(engine, time.monotonic() - 1, lambda conn: pytest.fail("过期工具不应查询"))


def test_token_budget_is_enforced(engine, owner, monkeypatch):
    monkeypatch.setattr("memory_agent.investigation.MAX_TOKENS", 1)
    with llm.use_models(investigate=FunctionModel(lambda m, i: tool("search_memories", {"question": "面试"}))), engine.begin() as c:
        result = investigate(c, space(engine, owner), "调查")
    assert result["abstain"] and result["stop_reason"] == "budget_exceeded"


def test_investigation_historical_tools_hide_future(engine, owner):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
        say(engine, s, "二面改到周四了", at(24))
    def model(messages, info):
        rs = tool_results(messages)
        if not rs:
            return tool("search_memories", {"question": "TapOn 二面什么时候？"})
        assert "2026-10-29" not in json.dumps([p.content for p in rs], ensure_ascii=False)
        mid = rs[0].content[0]["memory_id"]
        if len(rs) == 1:
            return tool("get_memory_history", {"memory_id": mid})
        if len(rs) == 2:
            return tool("get_source_evidence", {"memory_id": mid})
        return _call(info, {"answer": rs[0].content[0]["fact_text"], "used_memory_ids": [mid], "abstain": False})
    with llm.use_models(investigate=FunctionModel(model)), engine.begin() as c:
        result = investigate(c, s, "当时的面试安排", as_of=at(23, 12))
    assert "2026-10-29" not in json.dumps(result, ensure_ascii=False)
    assert "2026-10-27" in result["answer"]
