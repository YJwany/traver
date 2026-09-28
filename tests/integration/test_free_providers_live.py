"""Live end-to-end tests against the real free data services.

These hit OpenStreetMap/Overpass, Open-Meteo and OSRM for real. They are opt-in
because they depend on community infrastructure that is slow and occasionally
overloaded:

    $env:TRAVER_LIVE_TESTS = "1"; python -m pytest tests/integration/test_free_providers_live.py -q -s

Request discipline: the two tests below share one geocode per destination and
issue exactly one Overpass query each, which is the whole point of the combined
union query and the TTL cache.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.domain.models import DataDomain, SourceType, TripProfile
from travel_api.main import create_app
from travel_api.providers.free import build_free_providers
from travel_api.providers.free.geo import SOURCE_NOMINATIM, GeoResolver
from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.registry import ProviderRegistry
from travel_api.providers.resilience import ProviderCallPolicy, ProviderRuntime
from travel_api.services.aggregation import build_preview
from travel_api.services.entity_resolution import geo_distance_meters
from travel_api.services.normalization import normalize_name

pytestmark = pytest.mark.skipif(
    os.getenv("TRAVER_LIVE_TESTS") != "1",
    reason="live network tests are opt-in: set TRAVER_LIVE_TESTS=1",
)

DESTINATION = "成都"


def _trip_dates() -> tuple[date, date]:
    # Stay inside Open-Meteo's 16-day forecast horizon.
    start = date.today() + timedelta(days=1)
    return start, start + timedelta(days=3)


def _profile(**overrides) -> TripProfile:
    start, end = _trip_dates()
    payload = {
        "destination": DESTINATION,
        "start_date": start,
        "end_date": end,
        "travelers": 2,
        "total_budget": 6000,
        "pace": "balanced",
    }
    payload.update(overrides)
    return TripProfile(**payload)


def _registry_with_free_providers():
    providers, http = build_free_providers(timeout_seconds=120.0, request_timeout_seconds=30.0)
    registry = ProviderRegistry(
        providers,
        runtime=ProviderRuntime(
            ProviderCallPolicy(timeout_seconds=120, retry_count=2, backoff_seconds=1.0)
        ),
    )
    return registry, http


async def test_live_free_providers_aggregate_real_data():
    registry, http = _registry_with_free_providers()
    try:
        preview = await build_preview(_profile(), registry)
    finally:
        await http.aclose()

    # --- real places ------------------------------------------------------
    assert preview.attractions, "Overpass returned no attractions for 成都"
    assert preview.restaurants, "Overpass returned no restaurants for 成都"
    assert preview.hotels, "Overpass returned no hotels for 成都"

    for attraction in preview.attractions:
        assert attraction.canonical_name.strip()
        assert attraction.location.coordinates.latitude != 0
        # OSM is a geo source: it must never assert a review score.
        assert attraction.rating is None
        assert attraction.source_references
        assert attraction.source_references[0].source_type is SourceType.OPEN_DATA

    # --- real weather -----------------------------------------------------
    assert preview.weather is not None, "Open-Meteo returned no forecast"
    assert preview.weather.days
    for day in preview.weather.days:
        assert day.condition
        assert day.temperature_min_c <= day.temperature_max_c
    assert preview.weather.timezone

    # --- real routing -----------------------------------------------------
    assert preview.route_legs, "OSRM returned no route legs"
    for leg in preview.route_legs:
        assert leg.duration_minutes >= 1
        assert leg.source_references if hasattr(leg, "source_references") else True
        assert leg.source_reference.source_type is SourceType.OPEN_DATA
        assert leg.distance_meters >= 0
    # The routing matrix must contain both directions between real points.
    assert any(leg.distance_meters > 0 for leg in preview.route_legs)

    # Walking speeds must be physically plausible. The public OSRM demo serves
    # only the car profile and silently returns ~40 km/h for any profile name, so
    # this is the assertion that catches a pedestrian leg carrying a car time.
    walking_legs = [leg for leg in preview.route_legs if leg.distance_meters > 500]
    assert walking_legs
    for leg in walking_legs:
        speed_kmh = leg.distance_meters / 1000 / (leg.duration_minutes / 60)
        if leg.mode.value == "walk":
            assert 2.5 <= speed_kmh <= 6.5, (
                f"implausible walking speed {speed_kmh:.1f} km/h over "
                f"{leg.distance_meters} m in {leg.duration_minutes} min"
            )
            # A derived pedestrian time is never presented as measured.
            assert leg.is_estimate is True
            assert leg.cost is None
        else:
            assert 5 <= speed_kmh <= 120
            assert leg.is_estimate is False

    # --- realism of the itinerary -----------------------------------------
    itinerary_items = [item for day in preview.itinerary.days for item in day.items]
    assert itinerary_items, "the optimizer produced an empty itinerary"
    for item in itinerary_items:
        assert item.travel_from_previous_minutes >= 0
        assert item.end_time > item.start_time

    # --- normalization and deduplication on real data ----------------------
    # Normalization must produce usable entities: a display name, coordinates
    # that are not the 0,0 placeholder, and at least one provenance reference.
    for collection in (preview.attractions, preview.restaurants, preview.hotels):
        for entity in collection:
            assert entity.canonical_name.strip(), "entity lost its name in normalization"
            coordinates = entity.location.coordinates
            assert (coordinates.latitude, coordinates.longitude) != (0.0, 0.0)
            assert entity.source_references, "entity has no source reference"
            # OSM is a geo source and must not assert a review score.
            assert entity.rating is None

    # Every entity id must be unique within its collection.
    for collection in (preview.attractions, preview.restaurants, preview.hotels):
        ids = [entity.id for entity in collection]
        assert len(ids) == len(set(ids)), "duplicate entity ids survived dedup"

    # Dedup post-condition: two entities that name-match must not also be within
    # 500 m, otherwise they are the same place and should have been merged. This
    # mirrors _same_entity() and is what proves dedup actually ran.
    for label, collection in (
        ("attraction", preview.attractions),
        ("restaurant", preview.restaurants),
        ("hotel", preview.hotels),
    ):
        for index, left in enumerate(collection):
            for right in collection[index + 1 :]:
                name_a, name_b = (
                    normalize_name(left.canonical_name),
                    normalize_name(right.canonical_name),
                )
                if not (name_a == name_b or name_a in name_b or name_b in name_a):
                    continue
                distance = geo_distance_meters(
                    left.location.coordinates.latitude,
                    left.location.coordinates.longitude,
                    right.location.coordinates.latitude,
                    right.location.coordinates.longitude,
                )
                assert distance > 500, (
                    f"duplicate {label} not merged: {left.canonical_name!r} and "
                    f"{right.canonical_name!r} are {distance:.0f} m apart"
                )

    # --- decision engine --------------------------------------------------
    # Decisions can only exist for offers. With a geo-only source there are no
    # offers, so the engine must produce no decisions rather than inventing them.
    if not preview.hotel_offers:
        assert preview.hotel_decisions == [], "decisions appeared without any offer"
    else:
        assert preview.hotel_decisions
        for decision in preview.hotel_decisions:
            assert decision.score_breakdown is not None
            assert decision.explanations is not None

    # --- honesty about commercial facts -----------------------------------
    # OSM carries no rates, so no hotel offer may be fabricated.
    assert preview.hotel_offers == []
    by_category = {item.category: item for item in preview.budget.items}
    hotel_budget = by_category.get("hotel")
    assert hotel_budget is not None, "the hotel line must exist even without priced offers"
    assert hotel_budget.is_estimate is True, "hotel cost must be flagged as an estimate"

    # A geo-only data layer has no prices at all. Collapsing the budget to zero
    # would silently hide the traveller's largest cost, so every category must
    # carry a flagged allowance instead.
    assert preview.budget.total > 0, "budget must not collapse to zero without prices"
    for category in ("hotel", "food", "attraction", "transport"):
        assert category in by_category, f"missing budget category {category}"
        assert by_category[category].amount > 0, f"{category} must not be silently zero"
        assert by_category[category].is_estimate is True

    # No attraction price may be invented either.
    for attraction in preview.attractions:
        assert attraction.ticket_price is None or attraction.ticket_price.is_estimate is True

    # --- provenance -------------------------------------------------------
    sources = {ref.source for ref in preview.source_references}
    assert "openstreetmap" in sources
    assert "open-meteo" in sources
    assert "osrm" in sources

    osm_refs = [
        ref
        for ref in preview.source_references
        if ref.source_type is SourceType.OPEN_DATA and ref.domain is DataDomain.GEO_FACT
    ]
    assert osm_refs
    assert all(ref.request_context.get("attribution") for ref in osm_refs), (
        "every open-data reference must carry its attribution"
    )

    provider_names = {item.provider for item in preview.provider_health}
    assert {"openstreetmap", "open-meteo", "osrm"} <= provider_names


async def test_live_api_creates_and_persists_a_trip_from_real_data(tmp_path):
    start, end = _trip_dates()
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'live.db'}",
        auto_create_schema=True,
        free_providers_enabled=True,
        mock_provider_enabled=False,
        free_provider_timeout_seconds=120.0,
        # The runtime budget must be raised too. It defaults to 15 s, and a real
        # Overpass query was measured at 36.5 s, so leaving the default cut off a
        # legitimately slow provider and failed the request after ~35 s. Setting
        # only free_provider_timeout_seconds was not enough: the runtime policy
        # wraps every provider call and enforces its own, tighter limit.
        provider_timeout_seconds=120.0,
        provider_retry_count=1,
        provider_backoff_seconds=1.0,
    )

    body = {
        "destination": DESTINATION,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "travelers": 2,
        "total_budget": 6000,
        "pace": "balanced",
    }

    with TestClient(create_app(settings)) as client:
        created = client.post("/api/v1/trips", json=body)
        assert created.status_code == 200, created.text
        trip = created.json()

        loaded = client.get(f"/api/v1/trips/{trip['id']}")
        assert loaded.status_code == 200
        reloaded = loaded.json()

        itinerary = client.get(f"/api/v1/trips/{trip['id']}/itinerary")
        sources_response = client.get(f"/api/v1/trips/{trip['id']}/sources")
        providers = client.get("/api/v1/providers/health")

    preview = trip["preview"]
    assert preview["attractions"], "no real attractions reached the API response"
    assert preview["route_legs"], "no real route legs reached the API response"
    assert preview["weather"] is not None
    assert preview["weather"]["days"]

    # Persistence round-trip: the reloaded Trip must match what was created.
    assert reloaded["current_version"] == 1
    assert len(reloaded["preview"]["attractions"]) == len(preview["attractions"])
    assert reloaded["preview"]["weather"]["days"] == preview["weather"]["days"]
    assert len(reloaded["preview"]["route_legs"]) == len(preview["route_legs"])

    assert itinerary.status_code == 200
    assert len(itinerary.json()["days"]) == _profile().nights

    assert sources_response.status_code == 200
    assert sources_response.json()

    assert providers.status_code == 200
    health = {item["provider"]: item for item in providers.json()}
    assert "openstreetmap" in health
    assert health["openstreetmap"]["status"] in {"healthy", "degraded"}


async def test_live_nominatim_geocoding_and_user_agent_policy():
    """Nominatim is a named data source, so it needs direct live coverage.

    In normal operation Open-Meteo geocoding succeeds and Nominatim is never
    reached, so without this test the fallback path (and its User-Agent
    requirements) could be broken without anything noticing. The UA assertion
    matters because Nominatim answers HTTP 403 for an e-mail address in the UA.
    """
    http = FreeHttpClient(timeout_seconds=30.0, retry_count=3, retry_backoff_seconds=0.5)
    try:
        resolver = GeoResolver(http, sources=(SOURCE_NOMINATIM,))
        place = await resolver.resolve(DESTINATION)

        assert place.source_reference.source == "nominatim"
        assert place.source_reference.source_type is SourceType.OPEN_DATA
        assert place.source_reference.domain is DataDomain.GEO_FACT
        # Real coordinates for 成都, not a placeholder.
        assert 30.0 < place.latitude < 31.5, place.latitude
        assert 103.0 < place.longitude < 105.0, place.longitude
        assert place.name
        # The administrative bounding box is what Nominatim adds over Open-Meteo.
        assert place.bbox is not None
        south, west, north, east = place.bbox
        assert south < north and west < east
        assert south <= place.latitude <= north
        assert west <= place.longitude <= east
        # OSM attribution is mandatory.
        assert place.source_reference.request_context["attribution"]

        # The configured UA must be accepted (a 403 would have raised).
        assert http.user_agent
        assert "@" not in http.user_agent, (
            "an e-mail address in the User-Agent makes Nominatim return 403"
        )
    finally:
        await http.aclose()


async def test_live_nominatim_is_not_used_when_the_fallback_is_disabled():
    """Disabling the fallback must remove Nominatim from the resolver's sources."""
    providers, http = build_free_providers(nominatim_fallback=False)
    try:
        resolver = next(
            provider.geo_resolver
            for provider in providers
            if getattr(provider, "geo_resolver", None) is not None
        )
        assert resolver.sources == ("open-meteo",)
        place = await resolver.resolve(DESTINATION)
        assert place.source_reference.source == "open-meteo-geocoding"
    finally:
        await http.aclose()


# -- air quality / elevation (Open-Meteo) ----------------------------------


async def test_live_air_quality_and_elevation_come_from_open_meteo():
    """Air quality must really be fetched, not silently skipped.

    The forecast endpoint allows 16 days but the air-quality endpoint allows only
    7, so asking for the wrong horizon returns HTTP 400 and quietly yields no air
    data at all — which is exactly what happened before this was pinned down.
    Days beyond the air-quality horizon must be empty rather than defaulting to
    "clean air".
    """
    providers, http = build_free_providers(timeout_seconds=150.0, request_timeout_seconds=35.0)
    try:
        weather_provider = next(p for p in providers if getattr(p, "name", "") == "open-meteo")
        result = await weather_provider.search_weather(_profile())
        assert result.data, f"no forecast returned: {result.errors}"
        forecast = result.data[0]

        # Elevation arrives with the forecast payload; Chengdu sits near 500 m.
        assert forecast.elevation_m is not None
        assert 300 < forecast.elevation_m < 800, forecast.elevation_m

        days = forecast.days
        assert len(days) == 16, "the forecast keeps its own 16-day horizon"

        # The new fields must be populated for the first days, not all None.
        covered = [day for day in days if day.air_quality_index is not None]
        assert covered, (
            "no day carried an air-quality index; the request horizon is likely "
            "outside the endpoint's supported 0-7 day range"
        )
        for day in covered:
            assert day.air_quality_index >= 0
            assert day.pm2_5_ug_m3 is None or day.pm2_5_ug_m3 >= 0
            assert day.uv_index_max is None or day.uv_index_max >= 0

        # Air quality is only published for the first week; later days stay empty.
        later = days[10:]
        assert all(day.air_quality_index is None for day in later), (
            "days past the air-quality horizon must stay empty, not default to a value"
        )

        # The extra source must be recorded and attributed.
        air_refs = [
            reference
            for reference in forecast.source_references
            if "air-quality" in reference.source_id
        ]
        assert air_refs, "air quality must carry its own SourceReference"
        assert air_refs[0].domain is DataDomain.GEO_FACT
        assert air_refs[0].request_context["attribution"]

        # The verdict must be consistent with whatever data was returned.
        for day in days:
            if day.air_quality_index is not None and day.air_quality_index > 60:
                assert day.outdoor_friendly is False, (
                    f"{day.day_date} has poor air ({day.air_quality_index}) "
                    "but is still marked outdoor friendly"
                )
            if day.uv_index_max is not None and day.uv_index_max >= 8:
                assert day.outdoor_friendly is False
    finally:
        await http.aclose()


# -- holidays (local library, no network) ----------------------------------


async def test_live_holidays_over_a_real_chinese_holiday_period():
    """National Day must resolve to the full 调休 period, not a single date.

    This is the concrete reason the local MIT-licensed library replaced a web
    API: the API returned only 2026-10-01 and omitted the surrounding rest days.
    No network is required here, but it is grouped with the live tests because it
    asserts real-world calendar facts.
    """
    from datetime import date as date_type

    providers, http = build_free_providers(timeout_seconds=30.0, request_timeout_seconds=15.0)
    try:
        holiday_provider = next(p for p in providers if getattr(p, "name", "") == "holidays")
        profile = TripProfile(
            destination=DESTINATION,
            start_date=date_type(2026, 9, 30),
            end_date=date_type(2026, 10, 8),
            travelers=2,
        )
        result = await holiday_provider.search_holidays(profile)
        assert result.errors == []
        assert result.data
        calendar = result.data[0]

        assert calendar.country_code == "CN"
        national_day = calendar.on(date_type(2026, 10, 1))
        assert national_day is not None
        assert "国庆" in national_day.name

        # The bridge/rest days are the substance of the constraint.
        assert date_type(2026, 10, 2) in calendar.holiday_dates
        assert date_type(2026, 10, 3) in calendar.holiday_dates
        # 10-08 is outside the period.
        assert date_type(2026, 10, 8) not in calendar.holiday_dates

        # Qingming is a real Chinese public holiday the rejected API omitted.
        qingming = TripProfile(
            destination=DESTINATION,
            start_date=date_type(2026, 4, 1),
            end_date=date_type(2026, 4, 10),
            travelers=2,
        )
        qingming_calendar = (await holiday_provider.search_holidays(qingming)).data[0]
        assert any("清明" in holiday.name for holiday in qingming_calendar.holidays)
        assert qingming_calendar.source_references
    finally:
        await http.aclose()


# -- wikipedia content ----------------------------------------------------


async def test_live_wikipedia_describes_only_explicitly_linked_attractions():
    """Descriptions must come from real articles and only from explicit links.

    Asserted here rather than only offline because the whole design rests on the
    behaviour of the real OSM tags and the real MediaWiki API. A regression to
    name-based matching would show up as a description on an attraction that
    carries no OSM wikipedia/wikidata tag.
    """
    registry, http = _registry_with_free_providers()
    try:
        preview = await build_preview(_profile(), registry)
        attractions = preview.attractions
        assert len(attractions) > 10, "expected a real attraction set"

        def link_context(attraction) -> dict:
            merged: dict = {}
            for reference in attraction.source_references:
                if isinstance(reference.request_context, dict):
                    merged.update(reference.request_context)
            return merged

        linked = [
            a
            for a in attractions
            if link_context(a).get("wikipedia") or link_context(a).get("wikidata")
        ]
        described = [a for a in attractions if a.description]
        unlinked = [
            a
            for a in attractions
            if not link_context(a).get("wikipedia") and not link_context(a).get("wikidata")
        ]

        # The core safety property: nothing undescribed gets text.
        assert all(not a.description for a in unlinked), (
            "an attraction with no explicit OSM wikipedia/wikidata tag received a "
            f"description: {[a.canonical_name for a in unlinked if a.description]}"
        )

        if not linked:
            pytest.skip("no POI in this area carries an explicit wikipedia/wikidata tag")

        # Any description that does exist must be traceable to a real article.
        for attraction in described:
            assert attraction.wikipedia_url
            assert "wikipedia.org/wiki/" in attraction.wikipedia_url
            assert len(attraction.description) > 20
            content_refs = [
                reference
                for reference in attraction.source_references
                if reference.domain is DataDomain.CONTENT_REFERENCE
            ]
            assert content_refs, f"{attraction.canonical_name} has text with no content reference"
            # CC BY-SA requires attribution to travel with the text, and
            # request_context is a JSON column so the credit must be a string.
            attribution = content_refs[0].request_context.get("attribution")
            assert isinstance(attribution, str), (
                f"attribution must be a plain string for persistence, got "
                f"{type(attribution).__name__}"
            )
            assert "CC BY-SA 4.0" in attribution

        # Every linked attraction was attempted, so report the real hit rate.
        print(
            f"\nwikipedia: {len(described)}/{len(linked)} explicitly linked attractions described "
            f"(out of {len(attractions)} total)"
        )

        # The mandatory attribution set must include the new sources.
        response = None
        app = create_app(Settings(free_providers_enabled=False, mock_provider_enabled=True))
        with TestClient(app) as client:
            response = client.get("/api/v1/providers/attributions")
        assert response.status_code == 200
        providers_credited = {item["provider"] for item in response.json()}
        assert "wikipedia" in providers_credited
        assert "holidays" in providers_credited
    finally:
        await http.aclose()


async def test_live_os_attractions_expose_wikipedia_link_tags():
    """The enrichment depends entirely on OSM carrying explicit link tags.

    If a future Overpass query stops requesting those tags, enrichment silently
    yields nothing, so this guards the query itself against the real service.
    """
    registry, http = _registry_with_free_providers()
    try:
        preview = await build_preview(_profile(), registry)
        linked = []
        for attraction in preview.attractions:
            for reference in attraction.source_references:
                context = reference.request_context
                if isinstance(context, dict) and (
                    context.get("wikipedia") or context.get("wikidata")
                ):
                    linked.append((attraction.canonical_name, context))
                    break

        assert linked, (
            "no attraction carried a wikipedia/wikidata tag; the Overpass query "
            "may have stopped requesting them"
        )
        for _name, context in linked:
            # An element may carry only one of the two tags (安顺廊桥 has a
            # wikidata tag but no wikipedia tag), so neither key is guaranteed.
            assert context.get("wikipedia") or context.get("wikidata")
        print(f"\nOSM link tags found on {len(linked)} attractions, e.g. {linked[:3]}")
    finally:
        await http.aclose()
