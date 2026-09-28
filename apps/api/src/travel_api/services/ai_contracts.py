from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


class AIRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    system_instruction: str
    user_input: str
    context: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    response_format: dict[str, Any] | None = None
    max_tokens: int | None = None


class AIToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AIToolResult(BaseModel):
    tool_call_id: str
    name: str
    result: dict[str, Any]
    error: str | None = None


class AIResponse(BaseModel):
    request_id: str
    text: str | None = None
    tool_calls: list[AIToolCall] = Field(default_factory=list)
    tool_results: list[AIToolResult] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    estimated_cost: float | None = None
    success: bool = True
    error: str | None = None


class AIProvider(Protocol):
    async def complete(self, request: AIRequest) -> AIResponse: ...
