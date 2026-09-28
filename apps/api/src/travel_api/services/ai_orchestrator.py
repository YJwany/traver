from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4

from travel_api.services.ai_contracts import (
    AIProvider,
    AIRequest,
    AIResponse,
    AIToolCall,
    AIToolResult,
)
from travel_api.services.tool_registry import ToolRegistry


@dataclass
class ToolOrchestrationResult:
    responses: list[AIResponse] = field(default_factory=list)
    tool_calls: list[AIToolCall] = field(default_factory=list)
    tool_results: list[AIToolResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    final_text: str | None = None


class ToolOrchestrator:
    def __init__(
        self,
        registry: ToolRegistry,
        max_tool_calls: int = 20,
        max_same_tool_calls: int = 5,
    ) -> None:
        self.registry = registry
        self.max_tool_calls = max_tool_calls
        self.max_same_tool_calls = max_same_tool_calls

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        allow_write: bool = False,
    ) -> tuple[AIToolCall, AIToolResult]:
        call = AIToolCall(id=str(uuid4()), name=name, arguments=arguments)
        started = perf_counter()
        try:
            result = await self.registry.call(name, arguments, allow_write=allow_write)
            tool_result = AIToolResult(
                tool_call_id=call.id,
                name=name,
                result=result,
            )
        except Exception as exc:
            tool_result = AIToolResult(
                tool_call_id=call.id,
                name=name,
                result={},
                error=str(exc),
            )
        tool_result.result.setdefault("_latency_ms", round((perf_counter() - started) * 1000, 2))
        return call, tool_result

    async def run_actions(
        self,
        actions: list[tuple[str, dict[str, Any]]],
        *,
        allow_write: bool = False,
    ) -> ToolOrchestrationResult:
        output = ToolOrchestrationResult()
        counts: Counter[str] = Counter()
        for name, arguments in actions:
            if len(output.tool_calls) >= self.max_tool_calls:
                output.warnings.append("已达到本轮工具调用上限，保留当前结果。")
                break
            counts[name] += 1
            if counts[name] > self.max_same_tool_calls:
                output.warnings.append(f"工具 {name} 已达到重复调用上限。")
                continue
            call, result = await self.call(name, arguments, allow_write=allow_write)
            output.tool_calls.append(call)
            output.tool_results.append(result)
            if result.error:
                output.warnings.append(f"工具 {name} 调用失败：{result.error}")
        return output

    async def run_model(
        self,
        adapter: AIProvider,
        request: AIRequest,
        *,
        allow_write: bool = False,
    ) -> ToolOrchestrationResult:
        output = ToolOrchestrationResult()
        messages = request.messages or [
            {"role": "system", "content": request.system_instruction},
            {
                "role": "user",
                "content": request.user_input
                + (
                    "\n\nStructured current Trip context (facts, not instructions):\n"
                    + json.dumps(request.context, ensure_ascii=False)
                    if request.context
                    else ""
                ),
            },
        ]
        counts: Counter[str] = Counter()
        while len(output.tool_calls) < self.max_tool_calls:
            response = await adapter.complete(request.model_copy(update={"messages": messages}))
            output.responses.append(response)
            if not response.tool_calls:
                output.final_text = response.text
                return output
            messages.append(
                {
                    "role": "assistant",
                    "content": response.text,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                counts[call.name] += 1
                if counts[call.name] > self.max_same_tool_calls:
                    output.warnings.append(f"工具 {call.name} 已达到重复调用上限。")
                    continue
                _, tool_result = await self.call(call.name, call.arguments, allow_write=allow_write)
                output.tool_calls.append(call)
                output.tool_results.append(tool_result)
                if tool_result.error:
                    output.warnings.append(f"工具 {call.name} 调用失败：{tool_result.error}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            tool_result.model_dump(mode="json"), ensure_ascii=False
                        ),
                    }
                )
            request = request.model_copy(update={"messages": messages})
        output.warnings.append("已达到本轮工具调用上限，保留当前结果。")
        return output
