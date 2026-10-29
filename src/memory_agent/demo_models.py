"""免密钥规则演示，非大语言模型。只支持显式日期安排及简单偏好，不用作质量评测。"""
from __future__ import annotations

import json
import re
from datetime import date

from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel


def _input(messages):
    for message in reversed(messages):
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return json.loads(part.content)
    return {}


def _output(info, values):
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, values)])


def _extract(messages, info):
    current = _input(messages)["current_turn"]
    content = current["content"].strip().rstrip("。！! ")
    facts = []
    match = re.fullmatch(r"(.+?)(?:安排在|改到|改为|定在|在)\s*(\d{4}-\d{2}-\d{2})\s*(上午|下午|晚上)?", content)
    if match:
        subject, day, period = match.groups()
        try:
            date.fromisoformat(day)
        except ValueError:
            match = None
        else:
            value = day + (period or "")
            facts.append({"subject": subject, "predicate": "时间", "object_text": value,
                          "fact_text": f"{subject}在{value}", "resolved_date": day})
    preference = re.fullmatch(r"我(也)?喜欢(.+)", content)
    if preference:
        facts.append({"subject": "我", "predicate": "偏好", "object_text": preference[2], "fact_text": content})
    for fact in facts:
        fact["source_episode_ids"] = [current["episode_id"]]
    return _output(info, {"facts": facts})


def _update(messages, info):
    data = _input(messages)
    candidate = data["candidate"]
    for existing in data["existing"]:
        if (candidate["subject"], candidate["predicate"]) == (existing["subject"], existing["predicate"]):
            if "也喜欢" in candidate["fact_text"]:
                continue
            op = "NOOP" if candidate["object_text"] == existing["object_text"] else "INVALIDATE"
            return _output(info, {"op": op, "target_fact_id": existing["id"], "reason": "规则演示：比较同主体同属性的值"})
    return _output(info, {"op": "ADD", "reason": "规则演示：新增事实"})


def _answer_values(memories, question):
    # 演示程序不推断原因，也不把相似记忆伪装成问题的答案。
    if not memories or any(word in question for word in ("为什么", "原因", "why")):
        return {"answer": "没有足够的记忆依据回答这个问题", "used_memory_ids": [], "abstain": True}
    return {"answer": "；".join(m["fact_text"] for m in memories),
            "used_memory_ids": [m["memory_id"] for m in memories], "abstain": False}


def _answer(messages, info):
    data = _input(messages)
    return _output(info, _answer_values(data["memories"], data["question"]))


def _investigate(messages, info):
    data = _input(messages)
    returns = {}
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolReturnPart):
                content = part.content
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except ValueError:
                        content = []
                returns[part.tool_name] = content
    if "search_memories" not in returns:
        return ModelResponse(parts=[ToolCallPart("search_memories", {"question": data["question"]}, tool_call_id="demo-search")])
    memories = returns["search_memories"]
    if not isinstance(memories, list):
        memories = []
    if memories:
        memory_id = memories[0]["memory_id"]
        for name in ("get_memory_history", "get_source_evidence"):
            if name not in returns:
                return ModelResponse(parts=[ToolCallPart(name, {"memory_id": memory_id}, tool_call_id=f"demo-{name}")])
    return _output(info, _answer_values(memories, data["question"]))


def get_demo_model(role: str) -> FunctionModel:
    functions = {"extract": _extract, "update": _update, "answer": _answer, "investigate": _investigate}
    return FunctionModel(functions[role], model_name=f"demo-rules-{role}")
