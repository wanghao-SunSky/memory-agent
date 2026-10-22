"""trace：recall 与作业各一条 trace；默认不记录正文。"""
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from memory_agent import llm, tracing
from memory_agent.recall import recall
from tests.conftest import at
from tests.fakes import echo_answerer, gold_extractor, rule_updater
from tests.helpers import F, say, space

# 用不会出现在提示词/schema 里的独特字符串做探针
SECRET_TEXT = "Zorblax 公司三面后天上午"
G = {SECRET_TEXT: [F("Zorblax 三面", "时间", "2031-01-25 上午", "Zorblax 三面在 2031-01-25 上午")]}


def test_traces_have_no_content(engine, owner):
    exp = InMemorySpanExporter()
    tracing.setup_tracing("test", exporter=exp, simple=True)
    try:
        s = space(engine, owner)
        with llm.use_models(extract=gold_extractor(G), update=rule_updater(), answer=echo_answerer()):
            say(engine, s, SECRET_TEXT, at(23))
            with engine.begin() as c:
                r = recall(c, s, "Zorblax 三面安排在几号？")
        spans = exp.get_finished_spans()
        names = {sp.name for sp in spans}
        assert {"job.extract", "extract", "recall", "retrieval.vector"} <= names
        roots = [sp for sp in spans if sp.parent is None]
        assert {sp.name for sp in roots} >= {"job.extract", "recall"}
        assert format(next(sp for sp in roots if sp.name == "recall").context.trace_id, "032x") == r["trace_id"]
        # 有模型调用 span（Pydantic AI 的 GenAI 埋点）
        assert any(sp.attributes.get("gen_ai.operation.name") == "chat" or sp.name.startswith("chat") for sp in spans)
        blob = " ".join(f"{sp.name} {dict(sp.attributes)} {[e.attributes for e in sp.events]}" for sp in spans)
        for needle in ("Zorblax", "三面", "2031-01-25", "几号"):
            assert needle not in blob, f"trace 中出现了正文片段: {needle}"
    finally:
        tracing._provider = None
        from pydantic_ai import Agent
        Agent.instrument_all(False)
