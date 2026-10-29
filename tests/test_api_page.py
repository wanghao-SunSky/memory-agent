from pathlib import Path
import shutil
import subprocess

import pytest

from memory_agent import llm
from memory_agent.ui import PAGE
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
    page = client.get("/").text
    assert "你记得我什么" in page and "保存记忆" in page and "追查依据" in page
    assert "writeForm" in page and "questionForm" in page and "authForm" in page
    assert "2026-10-29" not in page  # 私有记忆由已认证 API 动态加载，不嵌入匿名 HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="UI async behavior checks need Node.js; no frontend packages required")
@pytest.mark.parametrize("scenario", [
    "connect_success", "connect_failure", "write_success", "write_failure",
    "delete_before_response", "delete_during_reload", "delete_failure",
    "retry_success", "retry_failure", "poll_after_switch", "poll_failure", "question_finally",
])
def test_ui_discards_async_responses_after_identity_or_space_switch(scenario):
    script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run(
        [shutil.which("node"), str(Path(__file__).with_name("ui_generation.cjs")), scenario],
        input=script, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
