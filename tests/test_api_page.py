import pytest

from memory_agent import llm
from tests.conftest import at
from tests.fakes import echo_answerer, gold_extractor, rule_updater
from tests.helpers import F, say, space

G = {
    "TapOn 二面下周二下午": [F("TapOn 二面", "时间", "2026-10-27 下午", "TapOn 二面在 2026-10-27（周二）下午")],
    "二面改到周四了": [F("TapOn 二面", "时间", "2026-10-29 下午", "TapOn 二面改到 2026-10-29（周四）下午")],
}


def test_memories_list_and_page(client, engine, owner):
    s = space(engine, owner)
    with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
        say(engine, s, "TapOn 二面下周二下午", at(23))
        say(engine, s, "二面改到周四了", at(24))
    m = client.get("/memories?space=work", headers={"X-Owner-Id": owner}).json()["memories"]
    assert len(m) == 1 and [v["status"] for v in m[0]["versions"]] == ["superseded", "active"]
    assert m[0]["versions"][0]["valid_to"] is not None and m[0]["current"]["sources"]
    page = client.get(f"/?space=work&owner={owner}").text
    assert "你记得我什么" in page and "已作废" in page and "删除" in page and "2026-10-29" in page
