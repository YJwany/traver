from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from travel_api.domain.models import (
    Attraction,
    BudgetStrategyName,
    HolidayCalendar,
    Hotel,
    HotelOffer,
    Pace,
    ProviderHealth,
    ProviderResult,
    Restaurant,
    RoutePoint,
    TripPreview,
    TripProfile,
    WeatherForecast,
)
from travel_api.providers.registry import ProviderRegistry
from travel_api.services.budget_engine import BudgetEngine
from travel_api.services.decision_engine import score_hotel_offers
from travel_api.services.entity_resolution import (
    deduplicate_attractions,
    deduplicate_hotels,
    deduplicate_restaurants,
)
from travel_api.services.normalization import (
    normalize_attractions,
    normalize_hotels,
    normalize_restaurants,
)
from travel_api.services.route_optimizer import RouteOptimizer

logger = logging.getLogger(__name__)

#: Upper bound on the routable point set. One OSRM table request returns the
#: full matrix, so this caps both request size and matrix growth (n^2 legs).
MAX_ROUTE_POINTS = 40


def _audit_entry(operation: str, result: ProviderResult) -> dict:
    payload = []
    for item in result.data:
        if hasattr(item, "model_dump"):
            payload.append(item.model_dump(mode="json", exclude_computed_fields=True))
        else:
            payload.append(item)
    return {
        "provider": result.provider,
        "operation": operation,
        "status": result.status,
        "errors": result.errors,
        "payload": payload,
        "data_count": len(result.data),
    }


async def _safe_call(registry: ProviderRegistry, provider, method: str, *args) -> ProviderResult:
    try:
        return await registry.call(provider, method, *args)
    except Exception as exc:
        logger.warning(
            "provider call failed: %s.%s: %s",
            provider.name,
            method,
            exc,
            # Do not hardcode request_id here: the logging filter fills it in
            # from the current request context. Hardcoding it made the most
            # useful diagnostic line uncorrelatable with the request that hit it.
            extra={"provider": provider.name, "method": method},
        )
        return ProviderResult(status="degraded", provider=provider.name, errors=[str(exc)])


async def _call_capability(registry: ProviderRegistry, method: str, *args) -> list[ProviderResult]:
    """Dispatch one capability to every provider that declares it."""
    providers = registry.providers_for(method)
    if not providers:
        return []
    return list(
        await asyncio.gather(
            *[_safe_call(registry, provider, method, *args) for provider in providers]
        )
    )


def _collect(results: list[ProviderResult], expected_type: type) -> list:
    return [item for result in results for item in result.data if isinstance(item, expected_type)]


def _build_route_points(
    selected_hotel: Hotel | None,
    attractions: list[Attraction],
    restaurants,
    profile: TripProfile,
    *,
    limit: int = MAX_ROUTE_POINTS,
) -> list[RoutePoint]:
    """Assemble the point set handed to the routing provider.

    The optimizer looks legs up by (origin_id, destination_id), so every place it
    may actually schedule must be in the matrix. Two properties matter:

    * Selection is *not* plain list order. The optimizer consumes attractions and
      restaurants from the front of each list, so a single shared allowance filled
      by one category leaves the others unroutable (previously the first 17
      attractions consumed everything, and every restaurant leg silently fell back
      to a hardcoded estimate).
    * The per-category budget is derived from how many entries an itinerary of
      this length can consume, with margin for candidates skipped because they do
      not fit, rather than a fixed count.
    """
    days = max(profile.nights, 1)
    attractions_per_day = 1 if profile.pace is Pace.RELAXED else 2
    # Pessimistic: the optimizer may walk past several candidates per slot before
    # finding one that fits, so allow generous margin over the scheduled count.
    attraction_budget = max(4, attractions_per_day * days * 3)
    restaurant_budget = max(4, days * 4)

    points: list[RoutePoint] = []
    seen: set = set()

    def add(entity) -> bool:
        if entity is None or entity.id in seen:
            return False
        coordinates = entity.location.coordinates
        seen.add(entity.id)
        points.append(
            RoutePoint(
                id=entity.id,
                latitude=coordinates.latitude,
                longitude=coordinates.longitude,
            )
        )
        return True

    add(selected_hotel)

    # Interleave so no single category can starve the others.
    attraction_slice = attractions[:attraction_budget]
    restaurant_slice = restaurants[:restaurant_budget]
    for index in range(max(len(attraction_slice), len(restaurant_slice))):
        if index < len(attraction_slice):
            add(attraction_slice[index])
        if index < len(restaurant_slice):
            add(restaurant_slice[index])

    return points[:limit]


async def build_preview(
    profile: TripProfile,
    registry: ProviderRegistry,
    budget_strategy: BudgetStrategyName | str = BudgetStrategyName.BALANCED,
    budget_engine: BudgetEngine | None = None,
) -> TripPreview:
    (
        hotel_results,
        restaurant_results,
        attraction_results,
        weather_results,
        holiday_results,
    ) = await asyncio.gather(
        _call_capability(registry, "search_hotels", profile),
        _call_capability(registry, "search_restaurants", profile),
        _call_capability(registry, "search_attractions", profile),
        _call_capability(registry, "search_weather", profile),
        _call_capability(registry, "search_holidays", profile),
    )

    hotels = deduplicate_hotels(normalize_hotels(_collect(hotel_results, Hotel)))
    hotel_ids = [str(hotel.id) for hotel in hotels]
    offer_results = (
        await _call_capability(registry, "search_room_offers", hotel_ids, profile)
        if hotel_ids
        else []
    )
    offers = _collect(offer_results, HotelOffer)

    restaurants = deduplicate_restaurants(
        normalize_restaurants(_collect(restaurant_results, Restaurant))
    )
    attractions = deduplicate_attractions(
        normalize_attractions(_collect(attraction_results, Attraction))
    )

    # Content enrichment runs *after* dedup so the same article is fetched once
    # per surviving attraction, and only for entities that carry an explicit
    # OSM wikipedia/wikidata link (the provider never guesses a match).
    enrichment_results = (
        await _call_capability(registry, "enrich_attractions", attractions, profile)
        if attractions
        else []
    )
    if enrichment_results:
        enriched = _collect(enrichment_results, Attraction)
        # Only accept the enriched copies for entities that actually exist, so a
        # misbehaving provider cannot introduce new attractions.
        known = {attraction.id for attraction in attractions}
        replacements = {item.id: item for item in enriched if item.id in known}
        attractions = [replacements.get(item.id, item) for item in attractions]

    decisions = score_hotel_offers(hotels, offers, profile)
    selected_offer = None
    if decisions and not decisions[0].failed_constraints:
        selected_offer = next(
            (offer for offer in offers if offer.id == decisions[0].candidate_id), None
        )
    selected_hotel = next(
        (hotel for hotel in hotels if selected_offer and hotel.id == selected_offer.hotel_id),
        None,
    )

    # Routing happens *after* the candidate set exists: a routing service needs
    # real coordinates, which are only known once POIs have been aggregated.
    route_points = _build_route_points(selected_hotel, attractions, restaurants, profile)
    route_results = (
        await _call_capability(registry, "calculate_routes", profile, route_points)
        if len(route_points) >= 2
        else []
    )
    routes = [
        item for result in route_results for item in result.data if hasattr(item, "origin_id")
    ]

    budget = (budget_engine or BudgetEngine()).calculate(
        profile,
        selected_offer,
        restaurants,
        attractions,
        routes,
        strategy=budget_strategy,
    )
    itinerary = RouteOptimizer().optimize(
        profile,
        selected_hotel.id if selected_hotel else None,
        (
            selected_hotel.location.coordinates.latitude,
            selected_hotel.location.coordinates.longitude,
        )
        if selected_hotel
        else (0.0, 0.0),
        attractions,
        restaurants,
        routes,
    )
    weather = next(iter(_collect(weather_results, WeatherForecast)), None)
    holidays = next(iter(_collect(holiday_results, HolidayCalendar)), None)

    health: list[ProviderHealth] = await registry.health()
    refs = [
        ref
        for result in [
            *hotel_results,
            *offer_results,
            *restaurant_results,
            *attraction_results,
            *route_results,
            *weather_results,
            *holiday_results,
            *enrichment_results,
        ]
        for ref in result.source_references
    ]
    provider_audit = (
        [_audit_entry("search_hotels", result) for result in hotel_results]
        + [_audit_entry("search_room_offers", result) for result in offer_results]
        + [_audit_entry("search_restaurants", result) for result in restaurant_results]
        + [_audit_entry("search_attractions", result) for result in attraction_results]
        + [_audit_entry("calculate_routes", result) for result in route_results]
        + [_audit_entry("search_weather", result) for result in weather_results]
        + [_audit_entry("search_holidays", result) for result in holiday_results]
        + [_audit_entry("enrich_attractions", result) for result in enrichment_results]
    )
    return TripPreview(
        profile=profile,
        hotels=hotels,
        hotel_offers=offers,
        hotel_decisions=decisions,
        restaurants=restaurants,
        attractions=attractions,
        route_legs=routes,
        budget=budget,
        itinerary=itinerary,
        weather=weather,
        holidays=holidays,
        provider_health=health,
        provider_audit=provider_audit,
        source_references=refs,
        generated_at=datetime.now(timezone.utc),
    )
