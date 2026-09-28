from __future__ import annotations

from typing import Protocol, runtime_checkable

from travel_api.domain.models import (
    Attraction,
    ProviderHealth,
    ProviderResult,
    RoutePoint,
    TripProfile,
)

# Capability protocols.
#
# A provider implements only the capabilities it can actually serve. The
# registry routes each aggregation call to the providers that declare the
# matching capability, so a geo-only source (OpenStreetMap) never has to fake
# commercial data it does not have, and a weather-only source never has to
# return empty hotel lists.


@runtime_checkable
class HotelProvider(Protocol):
    name: str

    async def search_hotels(self, profile: TripProfile) -> ProviderResult: ...

    async def search_room_offers(
        self, hotel_ids: list[str], profile: TripProfile
    ) -> ProviderResult: ...


@runtime_checkable
class FoodProvider(Protocol):
    name: str

    async def search_restaurants(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class AttractionProvider(Protocol):
    name: str

    async def search_attractions(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class RouteProvider(Protocol):
    name: str

    async def calculate_routes(
        self, profile: TripProfile, points: list[RoutePoint]
    ) -> ProviderResult: ...


@runtime_checkable
class WeatherProvider(Protocol):
    name: str

    async def search_weather(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class HolidayProvider(Protocol):
    """Supplies public holidays that overlap the trip.

    Kept as its own capability because it is a calendar fact, not a POI or a
    forecast: a source can serve holidays without serving anything else.
    """

    name: str

    async def search_holidays(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class ContentProvider(Protocol):
    """Enriches already-aggregated entities with encyclopedic text.

    Separate from ``AttractionProvider`` because it consumes entities that other
    providers produced rather than discovering new ones, so it runs after
    aggregation and entity resolution.
    """

    name: str

    async def enrich_attractions(
        self, attractions: list[Attraction], profile: TripProfile
    ) -> ProviderResult: ...


@runtime_checkable
class HealthProvider(Protocol):
    name: str

    async def health(self) -> ProviderHealth: ...


# Method name -> the capability a provider must expose to receive that call.
CAPABILITY_BY_METHOD: dict[str, type] = {
    "search_hotels": HotelProvider,
    "search_room_offers": HotelProvider,
    "search_restaurants": FoodProvider,
    "search_attractions": AttractionProvider,
    "calculate_routes": RouteProvider,
    "search_weather": WeatherProvider,
    "search_holidays": HolidayProvider,
    "enrich_attractions": ContentProvider,
}


class TravelProvider(
    HotelProvider,
    FoodProvider,
    AttractionProvider,
    RouteProvider,
    WeatherProvider,
    HolidayProvider,
    HealthProvider,
    Protocol,
):
    """Full-surface provider: serves every category from one dataset.

    Kept for mock/fixture providers that intentionally cover all categories.
    """

    name: str
