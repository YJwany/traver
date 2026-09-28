from __future__ import annotations

import json
import time
from typing import Any

import httpx

from travel_api.services.ai_contracts import AIRequest, AIResponse, AIToolCall


class AIAdapterError(RuntimeError):
    pass


class OpenAICompatibleAdapter:
    """Small HTTP adapter for OpenAI-compatible chat/tool-call APIs."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_tokens: int = 1200,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    async def complete(self, request: AIRequest) -> AIResponse:
        if not self.api_key:
            raise AIAdapterError("AI_API_KEY is not configured")
        messages = request.messages or [
            {"role": "system", "content": request.system_instruction},
            {"role": "user", "content": request.user_input},
        ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": request.max_tokens or self.max_tokens,
        }
        if request.tools:
            body["tools"] = request.tools
            body["tool_choice"] = "auto"
        if request.response_format:
            body["response_format"] = request.response_format

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AIAdapterError(str(exc)) from exc

        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = [self._parse_tool_call(item) for item in message.get("tool_calls", [])]
        usage = payload.get("usage") or {}
        return AIResponse(
            request_id=request.request_id,
            text=message.get("content"),
            tool_calls=tool_calls,
            model=payload.get("model", self.model),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    @staticmethod
    def _parse_tool_call(raw: dict[str, Any]) -> AIToolCall:
        function = raw.get("function") or {}
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        return AIToolCall(
            id=str(raw.get("id", "tool-call")),
            name=str(function.get("name", "")),
            arguments=arguments if isinstance(arguments, dict) else {},
        )
