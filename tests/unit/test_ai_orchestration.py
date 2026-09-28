import asyncio
from datetime import date

from pydantic import BaseModel
from travel_api.ai.openai_compatible import OpenAICompatibleAdapter
from travel_api.domain.models import ConversationMessage, ConversationRole, TripProfile
from travel_api.services.ai_contracts import AIRequest, AIResponse
from travel_api.services.ai_orchestrator import ToolOrchestrator
from travel_api.services.conversation_context import ConversationContextBuilder
from travel_api.services.tool_registry import ToolDefinition, ToolRegistry


class EmptyInput(BaseModel):
    pass


class FakeProvider:
    async def complete(self, request: AIRequest) -> AIResponse:
        del request
        return AIResponse(request_id="fake", text="已完成结构化解释", model="fake-model")


def test_orchestrator_enforces_same_tool_limit():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read",
            description="read",
            input_model=EmptyInput,
            output_model=None,
            permission="trip:read",
            handler=lambda payload: {"status": "success"},
        )
    )
    result = asyncio.run(
        ToolOrchestrator(registry, max_tool_calls=4, max_same_tool_calls=2).run_actions(
            [("read", {}), ("read", {}), ("read", {})]
        )
    )

    assert len(result.tool_calls) == 2
    assert result.warnings


def test_context_builder_keeps_current_trip_and_recent_messages():
    profile = TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 7),
        travelers=2,
    )
    from travel_api.domain.models import Trip

    trip = Trip(profile=profile)
    context = ConversationContextBuilder(recent_message_limit=1).build(
        trip,
        [
            ConversationMessage(role=ConversationRole.USER, content="old"),
            ConversationMessage(role=ConversationRole.USER, content="latest"),
        ],
    )

    assert context["trip"]["profile"]["destination"] == "成都"
    assert [item["content"] for item in context["recent_messages"]] == ["latest"]


def test_orchestrator_accepts_final_ai_response():
    registry = ToolRegistry()
    result = asyncio.run(
        ToolOrchestrator(registry).run_model(
            FakeProvider(),
            AIRequest(system_instruction="policy", user_input="hello"),
        )
    )

    assert result.final_text == "已完成结构化解释"
    assert result.responses[0].model == "fake-model"


def test_openai_compatible_adapter_parses_tool_calls(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "mock-model",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "search_hotels",
                                        "arguments": '{"destination":"成都"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "travel_api.ai.openai_compatible.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    response = asyncio.run(
        OpenAICompatibleAdapter("http://mock", "key", "mock-model").complete(
            AIRequest(system_instruction="policy", user_input="search")
        )
    )

    assert response.tool_calls[0].name == "search_hotels"
    assert response.tool_calls[0].arguments == {"destination": "成都"}
    assert response.input_tokens == 3
