"""确定性的离线模型（Pydantic AI FunctionModel）：抽取按金标，更新按规则，回答按检索结果拼接。"""
from __future__ import annotations

import json
from typing import Callable

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from memory_agent.engine import fact_key, normalize


def last_user_json(messages: list[ModelMessage]) -> dict:
    for m in reversed(messages):
        for p in reversed(getattr(m, "parts", [])):
            if isinstance(p, UserPromptPart) and isinstance(p.content, str):
                return json.loads(p.content)
    raise AssertionError("no user prompt")


def _call(info: AgentInfo, args: dict) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])


def gold_extractor(gold: dict[str, list[dict]]) -> FunctionModel:
    """gold: 本轮原文(打码后) -> 候选事实列表（不含 source_episode_ids）。未登记的原文抽不出事实。"""

    def fn(messages, info):
        data = last_user_json(messages)
        cur = data["current_turn"]
        facts = [dict(f, source_episode_ids=[cur["episode_id"]]) for f in gold.get(cur["content"], [])]
        return _call(info, {"facts": facts})

    return FunctionModel(fn, model_name="fake-gold-extractor")


def rule_updater(on_call: Callable[[dict], None] | None = None) -> FunctionModel:
    """同一 fact_key：值相同 NOOP，不同 INVALIDATE；“也……”表示可并存，ADD；否则 ADD。"""

    def fn(messages, info):
        data = last_user_json(messages)
        if on_call:
            on_call(data)
        c = data["candidate"]
        k = fact_key(c["subject"], c["predicate"])
        for e in data["existing"]:
            if fact_key(e["subject"], e["predicate"]) == k and not c["fact_text"].count("也"):  # “也喜欢 B”可并存
                if normalize(e["object_text"]) == normalize(c["object_text"]):
                    return _call(info, {"op": "NOOP", "target_fact_id": e["id"], "reason": "same key same value"})
                return _call(info, {"op": "INVALIDATE", "target_fact_id": e["id"], "reason": "same key new value"})
        return _call(info, {"op": "ADD", "target_fact_id": None, "reason": "new"})

    return FunctionModel(fn, model_name="fake-rule-updater")


def fixed_updater(decision: dict) -> FunctionModel:
    return FunctionModel(lambda m, info: _call(info, decision), model_name="fake-fixed-updater")


def echo_answerer() -> FunctionModel:
    """把检索到的当前有效记忆拼成回答（含已作废旧版本说明）；无记忆则弃答。"""

    def fn(messages, info):
        data = last_user_json(messages)
        mems = data["memories"]
        if not mems:
            return _call(info, {"answer": "没有相关记忆", "used_memory_ids": [], "abstain": True})
        parts = []
        for m in mems:
            s = m["fact_text"]
            if m["history"]:
                s += "（原定：" + "；".join(h["fact_text"] for h in m["history"]) + "，已作废）"
            parts.append(s)
        return _call(info, {"answer": " ".join(parts), "used_memory_ids": [m["memory_id"] for m in mems], "abstain": False})

    return FunctionModel(fn, model_name="fake-echo-answerer")
