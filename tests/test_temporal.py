import json
import importlib
from datetime import datetime

import pytest
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text

from memory_agent import llm
from memory_agent.recall import memory_details, recall, retrieve
from tests.conftest import at
from tests.fakes import echo_answerer, gold_extractor, rule_updater
from tests.helpers import say, space
from tests.test_recall import G


def test_as_of_uses_event_validity_without_future_version(engine, owner):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
        say(engine, s, "二面改到周四了", at(24))
        with engine.begin() as c:
            past = recall(c, s, "TapOn 二面什么时候？", as_of=at(23, 12))
            boundary = recall(c, s, "TapOn 二面什么时候？", as_of=at(24))
            now = recall(c, s, "TapOn 二面什么时候？")
    assert "2026-10-27" in past["answer"]
    assert "2026-10-29" not in json.dumps(past, ensure_ascii=False)
    assert past["memories"][0]["valid_to"] is None
    assert past["memories"][0]["history"] == []
    assert "2026-10-29" in boundary["answer"]
    assert "2026-10-29" in now["answer"]


def test_before_first_fact_abstains_without_model(engine, owner):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
    def forbidden(*_):
        raise AssertionError("historical query before first fact must not call model")
    with llm.use_models(answer=FunctionModel(forbidden)), engine.begin() as c:
        result = recall(c, s, "TapOn 二面什么时候？", as_of=at(22))
    assert result["abstain"] and not result["memories"]


def test_historical_retrieval_still_scopes_by_space(engine, owner):
    s = space(engine, owner)
    other = space(engine, owner, "life")
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
    with engine.begin() as c:
        assert retrieve(c, other, "TapOn 二面什么时候？", as_of=at(23, 12)) == []


def test_as_of_requires_timezone(engine, owner):
    s = space(engine, owner)
    with engine.begin() as c, pytest.raises(ValueError, match="timezone"):
        recall(c, s, "过去的安排", as_of=at(23).replace(tzinfo=None))


def freeze_clock(monkeypatch, when):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)

    monkeypatch.setattr(importlib.import_module("memory_agent.recall"), "datetime", FixedDatetime)


@pytest.mark.parametrize("when,expected", [(at(23, 12), "2026-10-27"), (at(24), "2026-10-29")])
def test_current_query_uses_effective_version_at_future_boundary(engine, owner, monkeypatch, when, expected):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
        say(engine, s, "二面改到周四了", at(24))
        freeze_clock(monkeypatch, when)
        with engine.begin() as c:
            current = recall(c, s, "TapOn 二面什么时候？")
            explicit = recall(c, s, "TapOn 二面什么时候？", as_of=when)
    assert expected in current["answer"]
    assert current["memories"] == explicit["memories"]
    if when < at(24):
        assert "2026-10-29" not in json.dumps(current, ensure_ascii=False)
        assert current["memories"][0]["valid_to"] is None
        assert current["memories"][0]["history"] == []


def test_default_details_hide_future_sources_and_version_metadata(engine, owner, monkeypatch):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        old = say(engine, s, "TapOn 二面下周二下午", at(23))
        future = say(engine, s, "二面改到周四了", at(24))
    freeze_clock(monkeypatch, at(23, 12))
    with engine.begin() as c:
        # A later confirmation may be linked to an older fact. It is still future evidence.
        c.execute(text("UPDATE facts SET source_episode_ids=array_append(source_episode_ids, :e) WHERE space_id=:s AND status='superseded'"),
                  {"e": future["episode_id"], "s": s})
        hits = retrieve(c, s, "TapOn 二面什么时候？")
        memories = memory_details(c, s, hits)
    assert len(memories) == 1
    assert [e["episode_id"] for e in memories[0]["sources"]] == [old["episode_id"]]
    assert memories[0]["valid_to"] is None
    assert memories[0]["history"] == []
    assert "2026-10-29" not in json.dumps(memories, ensure_ascii=False)


def test_default_query_before_first_future_fact_abstains(engine, owner, monkeypatch):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, s, "TapOn 二面下周二下午", at(24))
    freeze_clock(monkeypatch, at(23))
    def forbidden(*_):
        raise AssertionError("future-only memory must not reach the answer model")
    with llm.use_models(answer=FunctionModel(forbidden)), engine.begin() as c:
        result = recall(c, s, "TapOn 二面什么时候？")
    assert result["abstain"] and result["memories"] == []
