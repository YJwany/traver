"""Offline unit tests for the free providers.

A fake transport serves recorded-shape payloads captured from the real services,
so the mapping from vendor JSON to domain models is verified without the network.
The live end-to-end checks live in tests/integration/test_free_providers_live.py.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from travel_api.domain.models import (
    Attraction,
    DataDomain,
    Hotel,
    Restaurant,
    RoutePoint,
    SourceType,
    TravelMode,
    TripProfile,
    WeatherForecast,
)
from travel_api.providers.free import build_free_providers
from travel_api.providers.free.geo import SOURCE_NOMINATIM, SOURCE_OPEN_METEO, GeoResolver
from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.free.open_meteo import OpenMeteoWeatherProvider
from travel_api.providers.free.osrm import OsmRoutingProvider
from travel_api.providers.free.overpass import OverpassPoiProvider, _parse_star_classification

NO_RATE_LIMIT = {
    "geocoding-api.open-meteo.com": 0.0,
    "nominatim.openstreetmap.org": 0.0,
    "overpass-api.de": 0.0,
    "overpass.private.coffee": 0.0,
    "maps.mail.ru": 0.0,
    "router.project-osrm.org": 0.0,
    "api.open-meteo.com": 0.0,
}

#: Hermetic tests drive a MockTransport, so retrying a simulated failure only
#: adds sleeping time. Retry behaviour has its own dedicated tests below.
NO_RETRY = {"host_intervals": NO_RATE_LIMIT, "retry_count": 0}


def make_profile(**overrides) -> TripProfile:
    payload = {
        "destination": "成都",
        "start_date": date(2026, 10, 3),
        "end_date": date(2026, 10, 5),
        "travelers": 2,
    }
    payload.update(overrides)
    return TripProfile(**payload)


GEOCODE_PAYLOAD = {
    "results": [
        {
            "id": 7472191,
            "name": "Chengdu",
            "latitude": 26.36828,
            "longitude": 115.34289,
            "feature_code": "PPL",
            "country": "China",
            "population": 5000,
            "timezone": "Asia/Shanghai",
        },
        {
            "id": 1815286,
            "name": "成都",
            "latitude": 30.66667,
            "longitude": 104.06667,
            "feature_code": "PPLA",
            "country": "中国",
            "population": 13568357,
            "timezone": "Asia/Shanghai",
        },
    ]
}

OVERPASS_PAYLOAD = {
    "version": 0.6,
    "osm3s": {"timestamp_osm_base": "2026-09-27T16:13:33Z"},
    "elements": [
        {
            "type": "node",
            "id": 4786592337,
            "lat": 30.6777962,
            "lon": 104.0443055,
            "tags": {
                "name": "成都永陵博物馆",
                "name:en": "Chengdu Yongling Museum",
                "tourism": "museum",
                "opening_hours": "08:30-17:30",
                "addr:street": "永陵路",
                "addr:housenumber": "10号",
            },
        },
        {
            "type": "node",
            "id": 1756176916,
            "lat": 30.6466122,
            "lon": 104.0433717,
            "tags": {
                "amenity": "restaurant",
                "cuisine": "chinese;hotpot",
                "name": "老家子",
            },
        },
        {
            "type": "way",
            "id": 999001,
            "center": {"lat": 30.6598, "lon": 104.0633},
            "tags": {"tourism": "hotel", "name": "成都锦江宾馆", "stars": "5", "brand": "Jinjiang"},
        },
        # Unnamed elements must be dropped rather than surfaced as blank cards.
        {"type": "node", "id": 1, "lat": 30.65, "lon": 104.06, "tags": {"tourism": "attraction"}},
        # A category the provider does not serve.
        {"type": "node", "id": 2, "lat": 30.65, "lon": 104.06, "tags": {"amenity": "bank"}},
    ],
}

FORECAST_PAYLOAD = {
    "timezone": "Asia/Shanghai",
    "daily": {
        "time": ["2026-10-03", "2026-10-04"],
        "weather_code": [0, 65],
        "temperature_2m_max": [25.6, 18.2],
        "temperature_2m_min": [22.1, 15.0],
        "precipitation_sum": [0.0, 12.4],
        "precipitation_probability_max": [5, 88],
    },
}

OSRM_TABLE_PAYLOAD = {
    "code": "Ok",
    "distances": [[0, 4818], [5468.4, 0]],
    "durations": [[0, 426.6], [432.9, 0]],
}

#: Nominatim returns a bare JSON list (not an object) with string coordinates.
NOMINATIM_PAYLOAD = [
    {
        "place_id": 423858187,
        "osm_id": 2110264,
        "lat": "30.65984",
        "lon": "104.06516",
        "name": "成都",
        "display_name": "成都, 四川省, 中国",
        "boundingbox": ["30.547", "30.793", "103.9", "104.213"],
        "address": {"city": "成都", "state": "四川省", "country": "中国"},
    }
]


def _handler(*, overpass_fail_first_mirror: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=GEOCODE_PAYLOAD)
        if host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=NOMINATIM_PAYLOAD)
        if host == "overpass-api.de" and overpass_fail_first_mirror:
            return httpx.Response(504, text="server is probably too busy")
        if "overpass" in host or host == "maps.mail.ru":
            return httpx.Response(200, json=OVERPASS_PAYLOAD)
        if host == "api.open-meteo.com":
            return httpx.Response(200, json=FORECAST_PAYLOAD)
        if host == "router.project-osrm.org":
            return httpx.Response(200, json=OSRM_TABLE_PAYLOAD)
        return httpx.Response(404, text=f"unexpected host {host}{path}")

    return handler


def build_stack(*, overpass_fail_first_mirror: bool = False, **provider_kwargs):
    http = FreeHttpClient(
        user_agent="TraverTest/1.0",
        **NO_RETRY,
        transport=httpx.MockTransport(
            _handler(overpass_fail_first_mirror=overpass_fail_first_mirror)
        ),
    )
    geo = GeoResolver(http)
    poi = OverpassPoiProvider(http=http, geo_resolver=geo, **provider_kwargs)
    weather = OpenMeteoWeatherProvider(http=http, geo_resolver=geo)
    routing = OsmRoutingProvider(http=http)
    return http, geo, poi, weather, routing


# -- geocoding ------------------------------------------------------------


async def test_geocoder_prefers_the_populated_administrative_place():
    http, geo, *_ = build_stack()
    place = await geo.resolve("成都")
    assert place.latitude == pytest.approx(30.66667)
    assert place.timezone == "Asia/Shanghai"
    assert place.source_reference.source_type is SourceType.OPEN_DATA
    await http.aclose()


async def test_geocoder_caches_repeated_lookups():
    calls = {"count": 0}
    base = _handler()

    def counting_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            calls["count"] += 1
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(counting_handler))
    geo = GeoResolver(http)
    await geo.resolve("成都")
    await geo.resolve("成都")
    assert calls["count"] == 1
    await http.aclose()


async def test_nominatim_fallback_can_be_disabled():
    """FREE_PROVIDER_NOMINATIM_FALLBACK must actually change behaviour.

    It previously had no effect at all: the setting was never passed to the
    resolver, so configuring it to false still allowed Nominatim calls.
    """
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(_handler()))

    enabled = GeoResolver(http, nominatim_fallback_enabled=True)
    assert enabled.sources == (SOURCE_OPEN_METEO, SOURCE_NOMINATIM)

    disabled = GeoResolver(http, nominatim_fallback_enabled=False)
    assert disabled.sources == (SOURCE_OPEN_METEO,)
    await http.aclose()


async def test_nominatim_is_not_called_when_the_fallback_is_disabled():
    seen_hosts: list[str] = []
    base = _handler()

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(recording_handler))
    geo = GeoResolver(http, nominatim_fallback_enabled=False)
    await geo.resolve("成都")
    assert "nominatim.openstreetmap.org" not in seen_hosts
    await http.aclose()


async def test_geocoder_can_be_pinned_to_a_single_source():
    """An explicit source list must be honoured, which also makes each source testable."""
    seen_hosts: list[str] = []
    base = _handler()

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(recording_handler))
    geo = GeoResolver(http, sources=(SOURCE_NOMINATIM,))
    place = await geo.resolve("成都")
    assert place.source_reference.source == "nominatim"
    assert "geocoding-api.open-meteo.com" not in seen_hosts
    assert "nominatim.openstreetmap.org" in seen_hosts
    await http.aclose()


async def test_unknown_geocoding_source_is_rejected_loudly():
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(_handler()))
    with pytest.raises(ValueError, match="unknown geocoding source"):
        GeoResolver(http, sources=("google",))
    await http.aclose()


async def test_build_free_providers_threads_the_nominatim_setting():
    """The setting must reach the resolver that the providers actually use."""
    providers, http = build_free_providers(nominatim_fallback=False)
    try:
        resolvers = [
            provider.geo_resolver
            for provider in providers
            if getattr(provider, "geo_resolver", None) is not None
        ]
        assert resolvers, "expected providers sharing a GeoResolver"
        assert all(resolver.sources == (SOURCE_OPEN_METEO,) for resolver in resolvers)
        # And they must all share one resolver, so a destination is geocoded once.
        assert len({id(resolver) for resolver in resolvers}) == 1
    finally:
        await http.aclose()

    providers, http = build_free_providers(nominatim_fallback=True)
    try:
        resolver = next(
            provider.geo_resolver
            for provider in providers
            if getattr(provider, "geo_resolver", None) is not None
        )
        assert resolver.sources == (SOURCE_OPEN_METEO, SOURCE_NOMINATIM)
    finally:
        await http.aclose()


# -- overpass -------------------------------------------------------------


async def test_overpass_maps_tags_into_domain_entities():
    http, _, poi, *_ = build_stack()
    profile = make_profile()

    hotels = (await poi.search_hotels(profile)).data
    restaurants = (await poi.search_restaurants(profile)).data
    attractions = (await poi.search_attractions(profile)).data

    assert [item.canonical_name for item in hotels] == ["成都锦江宾馆"]
    assert isinstance(hotels[0], Hotel)
    assert hotels[0].brand == "Jinjiang"
    # OSM's "stars" is a classification, not a guest review score, so it must
    # not be written into `rating` (the decision engine scores rating / 5).
    assert hotels[0].rating is None
    assert hotels[0].review_count is None
    # The classification is preserved and auditable on the source reference.
    assert hotels[0].source_references[0].request_context["star_classification"] == 5

    assert [item.canonical_name for item in restaurants] == ["老家子"]
    assert isinstance(restaurants[0], Restaurant)
    assert restaurants[0].cuisine == ["chinese", "hotpot"]
    # OSM carries no meal prices, so no cost may appear.
    assert restaurants[0].average_cost is None

    assert [item.canonical_name for item in attractions] == ["成都永陵博物馆"]
    assert isinstance(attractions[0], Attraction)
    assert attractions[0].aliases == ["Chengdu Yongling Museum"]
    assert attractions[0].recommended_duration_minutes == 120
    assert len(attractions[0].opening_hours) == 7
    await http.aclose()


async def test_overpass_drops_unnamed_and_irrelevant_elements():
    http, _, poi, *_ = build_stack()
    profile = make_profile()
    attractions = (await poi.search_attractions(profile)).data
    assert len(attractions) == 1  # the unnamed attraction and the bank are gone
    await http.aclose()


async def test_overpass_never_invents_room_offers():
    http, _, poi, *_ = build_stack()
    result = await poi.search_room_offers([], make_profile())
    assert result.data == []
    assert result.status == "success"
    await http.aclose()


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("5", 5),
        ("4", 4),
        ("0", 0),
        ("4.0", 4),  # tolerated
        ("4.5", None),  # not a valid star classification
        ("6", None),  # out of range
        ("-1", None),
        ("", None),
        ("luxury", None),  # OSM values are not always clean
        (None, None),
    ],
)
def test_star_classification_parsing(tag, expected):
    assert _parse_star_classification(tag) == expected


async def test_osm_star_classification_is_not_reported_as_a_guest_rating():
    """A star classification must never be silently scored as a review score.

    The decision engine computes ``(hotel.rating or 0) / 5``, so writing OSM's
    ``stars`` into ``rating`` made a 5-star classification indistinguishable
    from a perfect 5.0 guest rating — and gave unrated hotels a score of 0.
    """
    http, _, poi, *_ = build_stack()
    hotels = (await poi.search_hotels(make_profile())).data

    starred = next(item for item in hotels if item.canonical_name == "成都锦江宾馆")
    assert starred.rating is None, "OSM star classification leaked into rating"
    assert starred.source_references[0].request_context["star_classification"] == 5

    # A hotel without a stars tag simply has no classification recorded.
    assert all(
        item.source_references[0].request_context.get("star_classification") is None
        for item in hotels
        if item.canonical_name != "成都锦江宾馆"
    )
    await http.aclose()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("50", "50"),
        ("50 CNY", "50"),
        ("¥50", "50"),
        ("1.5", "1.5"),
        ("+50", "50"),
        # A leading minus used to be skipped, turning a negative value positive:
        # a malformed classification became valid and a negative charge became a
        # real price. Both must be rejected instead.
        ("-50", None),
        ("-1", None),
        ("", None),
        ("free", None),
        (None, None),
    ],
)
def test_numeric_parsing_honours_a_leading_sign(raw, expected):
    from decimal import Decimal

    from travel_api.providers.free.overpass import _parse_decimal

    result = _parse_decimal(raw)
    assert result == (Decimal(expected) if expected is not None else None)


async def test_no_osm_hotel_exposes_a_guest_rating():
    """The invariant that keeps the decision engine honest.

    ``decision_engine`` computes ``(hotel.rating or 0) / 5``. With rating always
    None for OSM hotels, no free-source hotel can claim a rating advantage, and
    a star-classified hotel cannot silently score a perfect 1.0 on that
    criterion.
    """
    http, _, poi, *_ = build_stack()
    hotels = (await poi.search_hotels(make_profile())).data
    assert hotels
    assert {hotel.rating for hotel in hotels} == {None}
    await http.aclose()


async def test_overpass_uses_osm_snapshot_time_as_observation_time():
    http, _, poi, *_ = build_stack()
    hotels = (await poi.search_hotels(make_profile())).data
    reference = hotels[0].source_references[0]
    assert reference.observed_at.isoformat().startswith("2026-09-27T16:13:33")
    assert reference.domain is DataDomain.GEO_FACT
    assert reference.request_context["osm_id"] == 999001
    await http.aclose()


async def test_overpass_falls_back_to_the_next_mirror_on_504():
    http, _, poi, *_ = build_stack(
        overpass_fail_first_mirror=True,
        mirrors=[
            "https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
        ],
    )
    hotels = (await poi.search_hotels(make_profile())).data
    assert len(hotels) == 1
    assert poi.last_endpoint == "https://overpass.private.coffee/api/interpreter"
    await http.aclose()


async def test_overpass_issues_one_query_for_all_three_capabilities():
    """The three capability methods must share a single upstream request."""
    overpass_calls = {"count": 0}
    base = _handler()

    def counting_handler(request: httpx.Request) -> httpx.Response:
        if "overpass" in request.url.host or request.url.host == "maps.mail.ru":
            overpass_calls["count"] += 1
        return base(request)

    http = FreeHttpClient(
        host_intervals=NO_RATE_LIMIT, transport=httpx.MockTransport(counting_handler)
    )
    geo = GeoResolver(http)
    poi = OverpassPoiProvider(http=http, geo_resolver=geo)
    profile = make_profile()
    await poi.search_hotels(profile)
    await poi.search_restaurants(profile)
    await poi.search_attractions(profile)
    assert overpass_calls["count"] == 1
    await http.aclose()


# -- weather --------------------------------------------------------------


async def test_weather_maps_wmo_codes_and_flags_bad_outdoor_days():
    http, _, _, weather, _ = build_stack()
    result = await weather.search_weather(make_profile())
    forecast = result.data[0]
    assert isinstance(forecast, WeatherForecast)
    assert forecast.timezone == "Asia/Shanghai"
    assert [day.condition for day in forecast.days] == ["晴", "大雨"]
    assert forecast.days[0].outdoor_friendly is True
    assert forecast.days[1].outdoor_friendly is False
    assert forecast.days[1].precipitation_probability == 88
    assert forecast.source_references[0].source_type is SourceType.OPEN_DATA
    await http.aclose()


async def test_weather_warns_when_the_trip_is_beyond_the_forecast_horizon():
    http, _, _, weather, _ = build_stack()
    far_future = make_profile(start_date=date(2027, 6, 1), end_date=date(2027, 6, 5))
    result = await weather.search_weather(far_future)
    assert result.errors
    assert "超出天气预报范围" in result.errors[0]
    await http.aclose()


# -- routing --------------------------------------------------------------


async def test_routing_builds_legs_from_the_osrm_matrix():
    http, _, _, _, routing = build_stack()
    from uuid import uuid4

    first, second = uuid4(), uuid4()
    points = [
        RoutePoint(id=first, latitude=30.6558, longitude=104.0805),
        RoutePoint(id=second, latitude=30.6495, longitude=104.0432),
    ]
    result = await routing.calculate_routes(make_profile(), points)
    legs = result.data

    assert len(legs) == 2  # both directions, no self-pair
    forward = next(leg for leg in legs if leg.origin_id == first)
    assert forward.destination_id == second
    assert forward.distance_meters == 4818  # measured by OSRM
    assert forward.mode is TravelMode.WALK
    # Walking legs carry no fabricated fare.
    assert forward.cost is None
    await http.aclose()


async def test_walking_duration_is_derived_not_taken_from_car_routing():
    """The public OSRM demo routes cars only.

    Its 426.6 s figure for this 4.8 km leg is ~40 km/h. Presenting that as a walk
    would be a fabricated fact, so the provider must derive the duration from a
    walking speed and flag it as an estimate.
    """
    http, _, _, _, routing = build_stack()
    from uuid import uuid4

    first, second = uuid4(), uuid4()
    points = [
        RoutePoint(id=first, latitude=30.6558, longitude=104.0805),
        RoutePoint(id=second, latitude=30.6495, longitude=104.0432),
    ]
    result = await routing.calculate_routes(make_profile(), points)
    forward = next(leg for leg in result.data if leg.origin_id == first)

    # 4818 m at 1.25 m/s is about 64 minutes, not the 7 minutes the car
    # profile reported.
    assert forward.duration_minutes == 64
    assert forward.is_estimate is True

    # And the car figure must not have leaked in.
    assert forward.duration_minutes != 7

    reference = forward.source_reference
    assert reference.request_context["osrm_profile"] == "driving"
    assert reference.request_context["duration_source"] == "derived_walking_speed"
    await http.aclose()


async def test_driving_legs_use_measured_osrm_durations():
    http, _, _, _, routing = build_stack()
    from uuid import uuid4

    from travel_api.domain.models import Pace

    first, second = uuid4(), uuid4()
    points = [
        RoutePoint(id=first, latitude=30.6558, longitude=104.0805),
        RoutePoint(id=second, latitude=30.6495, longitude=104.0432),
    ]
    result = await routing.calculate_routes(make_profile(pace=Pace.FULL), points)
    forward = next(leg for leg in result.data if leg.origin_id == first)

    assert forward.mode is TravelMode.DRIVE
    # 426.6 s is a genuine car measurement and is used as-is.
    assert forward.duration_minutes == 7
    assert forward.is_estimate is False
    assert forward.cost is not None
    assert forward.cost.is_estimate is True
    assert forward.source_reference.request_context["duration_source"] == "osrm_car_profile"
    await http.aclose()


async def test_routing_never_requests_an_unsupported_profile():
    """Every profile name returns car data from the demo server, so it is not used."""
    seen_profiles: list[str] = []
    base = _handler()

    def recording_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "router.project-osrm.org":
            # /table/v1/<profile>/<coords>
            seen_profiles.append(request.url.path.split("/")[3])
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(recording_handler))
    routing = OsmRoutingProvider(http=http)
    from uuid import uuid4

    points = [
        RoutePoint(id=uuid4(), latitude=30.6558, longitude=104.0805),
        RoutePoint(id=uuid4(), latitude=30.6495, longitude=104.0432),
    ]
    from travel_api.domain.models import Pace

    await routing.calculate_routes(make_profile(pace=Pace.RELAXED), points)
    await routing.calculate_routes(make_profile(pace=Pace.FULL), points)
    assert seen_profiles == ["driving", "driving"]
    await http.aclose()


async def test_routing_needs_at_least_two_points():
    http, _, _, _, routing = build_stack()
    from uuid import uuid4

    result = await routing.calculate_routes(
        make_profile(), [RoutePoint(id=uuid4(), latitude=30.0, longitude=104.0)]
    )
    assert result.data == []
    await http.aclose()


async def test_routing_deduplicates_nothing_and_skips_self_pairs():
    """Every ordered pair except identity must appear exactly once."""
    http, _, _, _, routing = build_stack()
    from uuid import uuid4

    points = [
        RoutePoint(id=uuid4(), latitude=30.6558, longitude=104.0805),
        RoutePoint(id=uuid4(), latitude=30.6495, longitude=104.0432),
    ]
    result = await routing.calculate_routes(make_profile(), points)
    pairs = [(leg.origin_id, leg.destination_id) for leg in result.data]
    assert len(pairs) == len(set(pairs)) == 2
    assert all(origin != destination for origin, destination in pairs)
    await http.aclose()


# -- http layer -----------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_http_classifies_retryable_statuses(status):
    from travel_api.providers.resilience import ProviderTransientError

    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="busy"))
    http = FreeHttpClient(**NO_RETRY, transport=transport)
    with pytest.raises(ProviderTransientError):
        await http.get_json("https://api.open-meteo.com/v1/forecast")
    await http.aclose()


async def test_http_classifies_client_errors_as_permanent():
    from travel_api.providers.resilience import ProviderPermanentError

    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="nope"))
    http = FreeHttpClient(**NO_RETRY, transport=transport)
    with pytest.raises(ProviderPermanentError):
        await http.get_json("https://api.open-meteo.com/v1/forecast")
    await http.aclose()


async def test_http_classifies_transport_failures_as_transient():
    from travel_api.providers.resilience import ProviderTransientError

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(explode))
    with pytest.raises(ProviderTransientError):
        await http.get_json("https://api.open-meteo.com/v1/forecast")
    await http.aclose()


async def test_http_rejects_non_json_bodies():
    from travel_api.providers.resilience import ProviderPermanentError

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>not json</html>")
    )
    http = FreeHttpClient(**NO_RETRY, transport=transport)
    with pytest.raises(ProviderPermanentError):
        await http.get_json("https://api.open-meteo.com/v1/forecast")
    await http.aclose()


async def test_host_rate_limiter_enforces_a_minimum_gap():
    import asyncio
    import time

    from travel_api.providers.free.http import HostRateLimiter

    limiter = HostRateLimiter(0.05)
    started = time.monotonic()
    await asyncio.gather(*[limiter.acquire() for _ in range(3)])
    # Three serialized acquisitions need at least two gaps.
    assert time.monotonic() - started >= 0.09


async def test_http_retries_a_transient_failure_then_succeeds():
    """The public endpoints intermittently refuse the first connection.

    Without this retry a single blip fails the whole preview, so the behaviour is
    load-bearing rather than a nicety.
    """
    attempts = {"count": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"ok": True})

    http = FreeHttpClient(
        host_intervals=NO_RATE_LIMIT,
        transport=httpx.MockTransport(flaky),
        retry_count=2,
        retry_backoff_seconds=0.0,
    )
    assert await http.get_json("https://api.open-meteo.com/v1/forecast") == {"ok": True}
    assert attempts["count"] == 2
    await http.aclose()


async def test_http_retries_a_retryable_status():
    attempts = {"count": 0}

    def busy_then_ok(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    http = FreeHttpClient(
        host_intervals=NO_RATE_LIMIT,
        transport=httpx.MockTransport(busy_then_ok),
        retry_count=3,
        retry_backoff_seconds=0.0,
    )
    assert await http.get_json("https://api.open-meteo.com/v1/forecast") == {"ok": True}
    assert attempts["count"] == 3
    await http.aclose()


async def test_http_does_not_retry_permanent_failures():
    """Retrying a 404 only wastes a shared service's request budget."""
    from travel_api.providers.resilience import ProviderPermanentError

    attempts = {"count": 0}

    def not_found(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(404, text="nope")

    http = FreeHttpClient(
        host_intervals=NO_RATE_LIMIT,
        transport=httpx.MockTransport(not_found),
        retry_count=3,
        retry_backoff_seconds=0.0,
    )
    with pytest.raises(ProviderPermanentError):
        await http.get_json("https://api.open-meteo.com/v1/forecast")
    assert attempts["count"] == 1
    await http.aclose()


async def test_http_raises_after_exhausting_retries():
    from travel_api.providers.resilience import ProviderTransientError

    attempts = {"count": 0}

    def always_down(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    http = FreeHttpClient(
        host_intervals=NO_RATE_LIMIT,
        transport=httpx.MockTransport(always_down),
        retry_count=2,
        retry_backoff_seconds=0.0,
    )
    with pytest.raises(ProviderTransientError):
        await http.get_json("https://api.open-meteo.com/v1/forecast")
    assert attempts["count"] == 3  # initial attempt plus two retries
    await http.aclose()


async def test_geocoder_falls_back_when_the_first_source_is_down():
    """Geocoding gates every other free provider, so a source outage must not be fatal."""
    from travel_api.providers.free.geo import GeoResolutionError

    def nominatim_only(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(
                200,
                json=[
                    {
                        "lat": "30.65984",
                        "lon": "104.06516",
                        "name": "成都",
                        "osm_id": 123,
                        "display_name": "成都, 四川省, 中国",
                        "boundingbox": ["30.5", "30.8", "103.9", "104.2"],
                        "address": {"city": "成都", "country": "中国"},
                    }
                ],
            )
        return httpx.Response(404, text=f"unexpected host {request.url.host}")

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(nominatim_only))
    geo = GeoResolver(http)
    place = await geo.resolve("成都")
    assert place.city == "成都"
    assert place.source_reference.source == "nominatim"
    await http.aclose()

    # And when both sources are unreachable the error is explicit.
    def all_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(all_down))
    geo = GeoResolver(http)
    with pytest.raises(GeoResolutionError):
        await geo.resolve("成都")
    await http.aclose()


def test_overpass_query_is_bounded_and_has_per_category_limits():
    from travel_api.providers.free.overpass import OverpassPoiProvider

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(_handler()))
    provider = OverpassPoiProvider(
        http=http,
        geo_resolver=GeoResolver(http),
        hotel_limit=11,
        food_limit=22,
        attraction_limit=33,
    )
    query = provider._build_query("30.60,104.02,30.70,104.10")
    assert "nwr" in query
    bbox = "(30.60,104.02,30.70,104.10)"
    assert query.count(bbox) == 3
    # Each category needs its own budget, otherwise the first category in
    # element order consumes the whole allowance.
    assert ".hotels out center 11;" in query
    assert ".food out center 22;" in query
    assert ".sights out center 33;" in query
    json.dumps(query)  # must be serialisable as a form value
