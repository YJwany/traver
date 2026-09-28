from __future__ import annotations

from typing import Any

from travel_api.domain.models import ConversationMessage, Trip


class ConversationContextBuilder:
    def __init__(self, recent_message_limit: int = 12) -> None:
        self.recent_message_limit = recent_message_limit

    def build(self, trip: Trip | None, messages: list[ConversationMessage]) -> dict[str, Any]:
        trip_state: dict[str, Any] = {}
        if trip:
            trip_state = {
                "id": str(trip.id),
                "version": trip.current_version,
                "profile": trip.profile.model_dump(mode="json", exclude_computed_fields=True),
                "selected_hotel_offer_id": str(trip.selected_hotel_offer_id)
                if trip.selected_hotel_offer_id
                else None,
                "budget": trip.budget.model_dump(mode="json") if trip.budget else {},
                "itinerary": trip.itinerary.model_dump(mode="json"),
                "recent_sources": [
                    reference.model_dump(mode="json", exclude_computed_fields=True)
                    for reference in (trip.preview.source_references[:20] if trip.preview else [])
                ],
            }
        return {
            "trip": trip_state,
            "recent_messages": [
                {
                    "role": message.role.value,
                    "content": message.content,
                    "tool_name": message.tool_name,
                }
                for message in messages[-self.recent_message_limit :]
            ],
        }
