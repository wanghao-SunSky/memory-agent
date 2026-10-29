from memory_agent import llm
from memory_agent.recall import recall
from memory_agent.deletion import delete_memory
from tests.helpers import facts, say, space
from tests.conftest import at


def test_demo_write_update_answer_delete(engine, owner, monkeypatch):
    monkeypatch.setenv("MEMORY_MODEL_MODE", "demo")
    s = space(engine, owner)
    say(engine, s, "星河科技二面安排在2026-11-03下午", at(23))
    say(engine, s, "星河科技二面改到2026-11-05下午", at(24))
    current = facts(engine, s, "active")
    assert len(current) == 1
    with engine.begin() as c:
        answer = recall(c, s, "星河科技二面什么时候？")
    assert not answer["abstain"] and "2026-11-05" in answer["answer"]
    assert answer["used_memory_ids"] == [current[0]["id"]]
    assert delete_memory(engine, s, current[0]["id"])["result"] == "verified"
    with engine.begin() as c:
        assert recall(c, s, "星河科技二面什么时候？")["abstain"]


def test_demo_unsupported_text_does_not_invent_facts(engine, owner, monkeypatch):
    monkeypatch.setenv("MEMORY_MODEL_MODE", "demo")
    s = space(engine, owner)
    say(engine, s, "请给我写一首诗", at(23))
    assert facts(engine, s) == []


def test_live_missing_key_has_clear_failure(monkeypatch):
    monkeypatch.setenv("MEMORY_MODEL_MODE", "live")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(llm.config, "load_secrets", lambda: None)
    import pytest
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        llm.get_model("answer")
