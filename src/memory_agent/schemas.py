"""LLM 结构化输出的 Pydantic 模型（模型只返回这些结构，写库由代码执行）。"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class CandidateFact(BaseModel):
    subject: str = Field(description="主体，如 '某公司二面'、'我'、'某公司某岗位'")
    predicate: str = Field(description="属性，如 '时间'、'状态'、'地点'、'期望薪资'、'喜欢的方向'")
    object_text: str = Field(description="属性值。含日期时写绝对日期 YYYY-MM-DD")
    fact_text: str = Field(description="一句完整、可独立理解的中文事实，日期写成绝对日期")
    time_expression: str | None = Field(default=None, description="原文里的时间表达，如 '月底'、'下个月初'；没有则 null")
    resolved_date: date | None = Field(default=None, description="时间表达换算后的绝对日期；无法确定则 null，不要猜")
    valid_from: date | None = Field(default=None, description="这条事实在现实中从哪天开始成立；没说则 null（代码用本轮时间）")
    source_episode_ids: list[int] = Field(description="出处轮次 id，必须包含本轮 id")
    confidence: float = Field(default=0.9, ge=0, le=1)


class ExtractionResult(BaseModel):
    facts: list[CandidateFact] = Field(default_factory=list)


class UpdateDecision(BaseModel):
    op: Literal["ADD", "INVALIDATE", "NOOP"]
    target_fact_id: int | None = Field(default=None, description="INVALIDATE / NOOP 时指向的已有事实 id；ADD 为 null")
    reason: str = Field(description="一句话理由")


class RecallAnswer(BaseModel):
    answer: str = Field(description="中文回答；弃答时说明没有相关记忆")
    used_memory_ids: list[int] = Field(default_factory=list, description="回答实际用到的当前有效记忆 id")
    abstain: bool = Field(description="记忆不足以回答时为 true")
