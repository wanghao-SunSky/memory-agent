import pytest

from memory_agent import llm
from memory_agent.recall import recall
from pydantic_ai.models.function import FunctionModel
from tests.conftest import at
from tests.fakes import _call, echo_answerer, gold_extractor, rule_updater
from tests.helpers import F, say, space

G = {
    "TapOn 二面下周二下午": [F("TapOn 二面", "时间", "2026-10-27 下午", "TapOn 二面在 2026-10-27（周二）下午")],
    "二面改到周四了": [F("TapOn 二面", "时间", "2026-10-29 下午", "TapOn 二面改到 2026-10-29（周四）下午")],
    "周末去爬香山": [F("我", "周末计划", "爬香山", "用户周末要去爬香山")],
}


@pytest.fixture
def models():
    with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
        yield


def test_only_active_with_history(engine, owner, models):
    s = space(engine, owner)
    say(engine, s, "TapOn 二面下周二下午", at(23))
    say(engine, s, "二面改到周四了", at(24))
    with engine.begin() as c:
        r = recall(c, s, "TapOn 二面什么时候？")
    assert not r["abstain"] and "2026-10-29" in r["answer"]
    assert len(r["memories"]) == 1 and r["memories"][0]["history"][0]["status"] == "superseded"
    assert r["used_memory_ids"] == [r["memories"][0]["memory_id"]]


def test_empty_space_abstains_without_model_call(engine, owner):
    def must_not_call(m, info):
        raise AssertionError("不应调用回答模型")

    s = space(engine, owner, "life")
    with llm.use_models(answer=FunctionModel(must_not_call)), engine.begin() as c:
        r = recall(c, s, "TapOn 二面什么时候？")
    assert r["abstain"] and r["used_memory_ids"] == []


def test_space_isolation(engine, owner, models):
    w, l = space(engine, owner, "work"), space(engine, owner, "life")
    say(engine, w, "TapOn 二面下周二下午", at(23))
    say(engine, l, "周末去爬香山", at(23), session="life")
    with engine.begin() as c:
        rw = recall(c, w, "我周末要干什么")
        rl = recall(c, l, "TapOn 二面什么时候？")
    assert all("香山" not in m["fact_text"] for m in rw["memories"])
    assert rl["abstain"] and rl["memories"] == []


def test_used_ids_are_validated(engine, owner):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
    bad = FunctionModel(lambda m, info: _call(info, {"answer": "x", "used_memory_ids": [424242], "abstain": False}))
    with llm.use_models(answer=bad), engine.begin() as c:
        r = recall(c, s, "TapOn 二面什么时候？")
    assert r["used_memory_ids"] == []
    assert r["abstain"] is True
    assert r["answer"] != "x"


@pytest.mark.parametrize("refs,abstain", [([], False), ([424242], False), ([424242], True)])
def test_no_evidence_never_leaks_model_answer(engine, owner, refs, abstain):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
    bad = FunctionModel(lambda m, info: _call(info, {"answer": "没有依据的确定结论", "used_memory_ids": refs, "abstain": abstain}))
    with llm.use_models(answer=bad), engine.begin() as c:
        r = recall(c, s, "TapOn 二面什么时候？")
    assert r["abstain"] is True
    assert r["used_memory_ids"] == []
    assert "确定结论" not in r["answer"]


@pytest.mark.parametrize("abstain", [False, True])
def test_mixed_valid_ids_and_explicit_abstention(engine, owner, abstain):
    from tests.helpers import facts

    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
    mid = facts(engine, s)[0]["id"]
    answer = "TapOn 二面在 2026-10-27 下午"
    fake = FunctionModel(lambda m, info: _call(info, {"answer": answer, "used_memory_ids": [mid, 424242, mid], "abstain": abstain}))
    with llm.use_models(answer=fake), engine.begin() as c:
        r = recall(c, s, "TapOn 二面什么时候？")
    assert r["abstain"] is abstain
    assert r["used_memory_ids"] == ([] if abstain else [mid])
    assert (r["answer"] == answer) is (not abstain)


def test_api_recall(client, engine, owner, models):
    s = space(engine, owner)
    say(engine, s, "TapOn 二面下周二下午", at(23))
    r = client.post("/recall", json={"space": "work", "question": "TapOn 二面什么时候"}, headers={"X-Owner-Id": owner}).json()
    assert set(r) >= {"answer", "used_memory_ids", "abstain", "memories", "latency_ms"}


def test_blank_answer_is_not_a_successful_response():
    from memory_agent.recall import enforce_evidence
    from memory_agent.schemas import RecallAnswer

    result = enforce_evidence(RecallAnswer(answer="  ", used_memory_ids=[12], abstain=False), {12})
    assert result.abstain and result.used_memory_ids == []
    assert result.answer.strip()
