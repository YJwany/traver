"""Capability-based provider routing.

Free sources are partial by design: OpenStreetMap has no prices, Open-Meteo has
no places. The registry must send each call only to providers that declare the
capability, so a geo-only source is never asked to serve commercial data.
"""

from __future__ import annotations

from travel_api.domain.models import ProviderHealth, ProviderResult, RoutePoint, TripProfile
from travel_api.providers.registry import ProviderRegistry


class HotelsOnly:
    name = "hotels-only"

    async def health(self):
        return ProviderHealth(provider=self.name, status="healthy")

    async def search_hotels(self, profile):
        return ProviderResult(provider=self.name, data=["hotel"])

    async def search_room_offers(self, hotel_ids, profile):
        return ProviderResult(provider=self.name, data=["offer"])


class WeatherOnly:
    name = "weather-only"

    async def health(self):
        return ProviderHealth(provider=self.name, status="healthy")

    async def search_weather(self, profile):
        return ProviderResult(provider=self.name, data=["weather"])


class Everything:
    name = "everything"

    async def health(self):
        return ProviderHealth(provider=self.name, status="healthy")

    async def search_hotels(self, profile):
        return ProviderResult(provider=self.name)

    async def search_room_offers(self, hotel_ids, profile):
        return ProviderResult(provider=self.name)

    async def search_restaurants(self, profile):
        return ProviderResult(provider=self.name)

    async def search_attractions(self, profile):
        return ProviderResult(provider=self.name)

    async def search_weather(self, profile):
        return ProviderResult(provider=self.name)

    async def calculate_routes(self, profile, points):
        return ProviderResult(provider=self.name)


def _registry():
    return ProviderRegistry([HotelsOnly(), WeatherOnly(), Everything()])


def test_hotel_capability_routes_to_hotel_providers_only():
    names = [provider.name for provider in _registry().providers_for("search_hotels")]
    assert names == ["hotels-only", "everything"]


def test_weather_capability_routes_to_weather_providers_only():
    names = [provider.name for provider in _registry().providers_for("search_weather")]
    assert names == ["weather-only", "everything"]


def test_routing_capability_excludes_providers_without_it():
    names = [provider.name for provider in _registry().providers_for("calculate_routes")]
    assert names == ["everything"]


def test_restaurant_capability_skips_partial_providers():
    names = [provider.name for provider in _registry().providers_for("search_restaurants")]
    assert names == ["everything"]


def test_unknown_method_falls_back_to_every_provider():
    names = [provider.name for provider in _registry().providers_for("some_custom_capability")]
    assert names == ["hotels-only", "weather-only", "everything"]


def test_partial_provider_never_receives_a_capability_it_lacks():
    registry = _registry()
    assert registry.providers_for("search_room_offers") == [
        provider for provider in registry.all() if provider.name in {"hotels-only", "everything"}
    ]


async def test_health_reports_every_registered_provider():
    health = await _registry().health()
    assert {item.provider for item in health} == {"hotels-only", "weather-only", "everything"}


def test_registering_a_duplicate_name_is_rejected():
    registry = _registry()
    try:
        registry.register(HotelsOnly())
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate registration must fail")


def test_provider_without_a_name_is_rejected():
    class Anonymous:
        pass

    try:
        ProviderRegistry([Anonymous()])
    except ValueError as exc:
        assert "name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("anonymous provider must fail")


def test_route_point_carries_coordinates_for_routing():
    from uuid import uuid4

    point = RoutePoint(id=uuid4(), latitude=30.65, longitude=104.06)
    assert point.coordinates.latitude == 30.65
    assert point.coordinates.longitude == 104.06


async def test_circuit_breaker_isolates_a_failing_provider():
    from travel_api.providers.resilience import (
        ProviderCallPolicy,
        ProviderCircuitOpenError,
        ProviderRuntime,
        ProviderTransientError,
    )

    class AlwaysDown:
        name = "always-down"

        async def search_hotels(self, profile):
            raise ProviderTransientError("down")

    runtime = ProviderRuntime(
        ProviderCallPolicy(
            timeout_seconds=1.0,
            retry_count=0,
            backoff_seconds=0.0,
            circuit_breaker_threshold=2,
            circuit_breaker_reset_seconds=60,
        )
    )
    provider = AlwaysDown()
    for _ in range(2):
        try:
            await runtime.call(provider, "search_hotels", TripProfile)
        except ProviderTransientError:
            pass
    try:
        await runtime.call(provider, "search_hotels", TripProfile)
    except ProviderCircuitOpenError:
        pass
    else:  # pragma: no cover
        raise AssertionError("circuit should be open after the threshold")
    assert runtime.is_circuit_open("always-down")
