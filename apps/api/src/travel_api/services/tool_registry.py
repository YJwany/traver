from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None
    permission: str
    handler: Callable[[BaseModel], Any]
    kind: str = "read"

    def __post_init__(self) -> None:
        """Reject a definition whose permission and kind disagree.

        These two fields jointly decide whether the write gate applies, and a
        mismatch silently disables it: ``refresh_prices`` declared
        ``permission="trip:write"`` but left ``kind`` at its ``"read"`` default,
        so it was treated as a read tool. Deriving one from the other, or
        checking them at registration, makes that class of bug impossible
        rather than merely fixed once.
        """
        declared_write = self.permission.endswith(":write")
        if declared_write != (self.kind == "write"):
            raise ValueError(
                f"tool {self.name!r} is inconsistent: permission={self.permission!r} "
                f"implies kind={'write' if declared_write else 'read'}, "
                f"but kind={self.kind!r}"
            )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_model.model_json_schema(),
                "output_schema": definition.output_model.model_json_schema()
                if definition.output_model
                else None,
                "permission": definition.permission,
            }
            for definition in self._tools.values()
        ]

    def openai_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.input_model.model_json_schema(),
                },
            }
            for definition in self._tools.values()
        ]

    async def call(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        definition = self.get(name)
        if definition.kind == "write" and not allow_write:
            raise PermissionError(f"write tool requires explicit user intent: {name}")
        validated = definition.input_model.model_validate(payload)
        result = definition.handler(validated)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, BaseModel):
            return result.model_dump(mode="json")
        if isinstance(result, dict):
            return result
        return {"status": "success", "data": result}


class EmptyToolInput(BaseModel):
    pass


class TripIdToolInput(BaseModel):
    trip_id: str


class RevisionToolInput(BaseModel):
    trip_id: str
    action: str
    constraints: dict[str, Any] = Field(default_factory=dict)


class ToolStatusOutput(BaseModel):
    status: str = "success"
    data: dict[str, Any] | list[Any] = Field(default_factory=dict)


def build_default_tool_registry(trip_service) -> ToolRegistry:
    from travel_api.domain.models import (
        TripProfile,
        TripRevisionRequest,
        TripRevisionResult,
    )

    registry = ToolRegistry()

    async def get_budget(payload: TripIdToolInput):
        from uuid import UUID

        trip = trip_service.get(UUID(payload.trip_id))
        return ToolStatusOutput(data=trip.budget.model_dump(mode="json") if trip.budget else {})

    async def refresh_prices(payload: TripIdToolInput):
        from uuid import UUID

        trip = await trip_service.refresh(UUID(payload.trip_id))
        return ToolStatusOutput(data=trip.model_dump(mode="json"))

    async def search_hotels(payload: TripProfile):
        preview = await trip_service.preview(payload)
        return ToolStatusOutput(data=[item.model_dump(mode="json") for item in preview.hotels])

    async def search_restaurants(payload: TripProfile):
        preview = await trip_service.preview(payload)
        return ToolStatusOutput(data=[item.model_dump(mode="json") for item in preview.restaurants])

    async def search_attractions(payload: TripProfile):
        preview = await trip_service.preview(payload)
        return ToolStatusOutput(data=[item.model_dump(mode="json") for item in preview.attractions])

    async def get_hotel_offers(payload: TripProfile):
        preview = await trip_service.preview(payload)
        return ToolStatusOutput(
            data=[item.model_dump(mode="json") for item in preview.hotel_offers]
        )

    async def calculate_route(payload: TripProfile):
        preview = await trip_service.preview(payload)
        return ToolStatusOutput(data=preview.itinerary.model_dump(mode="json"))

    async def replace_hotel(payload: RevisionToolInput):
        from uuid import UUID

        result = await trip_service.revise(
            UUID(payload.trip_id),
            TripRevisionRequest(action="replace_hotel", constraints=payload.constraints),
        )
        return result

    async def revise_trip(payload: RevisionToolInput):
        from uuid import UUID

        result = await trip_service.revise(
            UUID(payload.trip_id),
            TripRevisionRequest(action=payload.action, constraints=payload.constraints),
        )
        return result

    registry.register(
        ToolDefinition(
            name="get_budget",
            description="Return the programmatically calculated budget for a persisted trip.",
            input_model=TripIdToolInput,
            output_model=ToolStatusOutput,
            permission="trip:read",
            handler=get_budget,
        )
    )
    for name, description, handler in (
        (
            "search_hotels",
            "Search normalized hotel candidates from registered Providers.",
            search_hotels,
        ),
        (
            "search_restaurants",
            "Search normalized restaurant candidates from registered Providers.",
            search_restaurants,
        ),
        (
            "search_attractions",
            "Search normalized attraction candidates from registered Providers.",
            search_attractions,
        ),
        (
            "get_hotel_offers",
            "Return room-level hotel offers with source and freshness context.",
            get_hotel_offers,
        ),
        (
            "calculate_route",
            "Calculate a structured itinerary using time windows and buffers.",
            calculate_route,
        ),
    ):
        registry.register(
            ToolDefinition(
                name=name,
                description=description,
                input_model=TripProfile,
                output_model=ToolStatusOutput,
                permission="trip:read",
                handler=handler,
            )
        )
    registry.register(
        ToolDefinition(
            name="replace_hotel",
            description="Replace the selected hotel through the structured revision engine.",
            input_model=RevisionToolInput,
            output_model=TripRevisionResult,
            permission="trip:write",
            handler=replace_hotel,
            kind="write",
        )
    )
    registry.register(
        ToolDefinition(
            name="refresh_prices",
            description="Refresh provider facts and persist new offer snapshots for a trip.",
            input_model=TripIdToolInput,
            output_model=ToolStatusOutput,
            permission="trip:write",
            handler=refresh_prices,
            kind="write",
        )
    )
    registry.register(
        ToolDefinition(
            name="revise_trip",
            description="Apply a structured local trip revision and persist change history.",
            input_model=RevisionToolInput,
            output_model=TripRevisionResult,
            permission="trip:write",
            handler=revise_trip,
            kind="write",
        )
    )
    return registry
