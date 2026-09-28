import asyncio

import pytest
from pydantic import BaseModel
from travel_api.services.tool_registry import ToolDefinition, ToolRegistry


class Input(BaseModel):
    value: str


def test_write_tool_requires_explicit_permission():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="mutate",
            description="mutate state",
            input_model=Input,
            output_model=None,
            permission="trip:write",
            handler=lambda payload: {"value": payload.value},
            kind="write",
        )
    )

    with pytest.raises(PermissionError):
        asyncio.run(registry.call("mutate", {"value": "x"}))

    assert asyncio.run(registry.call("mutate", {"value": "x"}, allow_write=True)) == {"value": "x"}


def test_openai_tool_definitions_are_function_schemas():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_value",
            description="read a value",
            input_model=Input,
            output_model=None,
            permission="trip:read",
            handler=lambda payload: payload.value,
        )
    )

    definition = registry.openai_definitions()[0]
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "read_value"
    assert "properties" in definition["function"]["parameters"]
