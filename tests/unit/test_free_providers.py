"""Offline unit tests for the free providers.

A fake transport serves recorded-shape payloads captured from the real services,
so the mapping from vendor JSON to domain models is verified without the network.
The live end-to-end checks live in tests/integration/test_free_providers_live.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from travel_api.domain.models import (
    Attraction,
    DataDomain,
    ElementAgeKind,
    Hotel,
    ObservationBasis,
    Pace,
    Restaurant,
    RoutePoint,
    SourceType,
    TravelMode,
    TripProfile,
    WeatherForecast,
)
from travel_api.providers.base import unserved_aspects_of
from travel_api.providers.free import build_free_providers
from travel_api.providers.free.geo import (
    SOURCE_NOMINATIM,
    SOURCE_OPEN_METEO,
    GeoResolver,
    _feature_rank,
    _score_open_meteo_result,
)
from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.free.open_meteo import MAX_FORECAST_DAYS, OpenMeteoWeatherProvider
from travel_api.providers.free.osrm import OsmRoutingProvider
from travel_api.providers.free.overpass import OverpassPoiProvider, _parse_star_classification

NO_RATE_LIMIT = {
    "geocoding-api.open-meteo.com": 0.0,
    "nominatim.openstreetmap.org": 0.0,
    "overpass-api.de": 0.0,
    "overpass.private.coffee": 0.0,
    "maps.mail.ru": 0.0,
    "router.project-osrm.org": 0.0,
    "routing.openstreetmap.de": 0.0,
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
            # `meta` timestamps, as `out center meta` really returns them. The ages here are
            # deliberately far apart, because that spread is the fact the provider now reports:
            # a snapshot minutes old can hold facts edited years ago.
            "timestamp": "2019-03-14T08:21:05Z",
            "version": 9,
            "changeset": 68123456,
            "user": "mapper-a",
            "uid": 1001,
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
            "timestamp": "2026-08-02T11:00:00Z",
            "version": 3,
            "changeset": 170000001,
            "user": "mapper-b",
            "uid": 1002,
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
            "timestamp": "2021-11-30T02:15:44Z",
            "version": 5,
            "changeset": 114000000,
            "user": "mapper-c",
            "uid": 1003,
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

#: What a real foot profile answers with, for the same pair of points.
#:
#: Deliberately different from the car matrix in **both** distance and duration, because that
#: difference is the only evidence available that a profile is genuinely a different mode
#: rather than the car graph relabelled. 4818 m on the car network against 3900 m on the foot
#: network is what pedestrians actually experience.
OSRM_FOOT_TABLE_PAYLOAD = {
    "code": "Ok",
    "distances": [[0, 3900], [3950, 0]],
    "durations": [[0, 3120], [3160, 0]],
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


def _handler(
    *,
    overpass_fail_first_mirror: bool = False,
    overpass_omit_osm_base: bool = False,
    overpass_omit_meta: bool = False,
):
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
            payload = OVERPASS_PAYLOAD
            if overpass_omit_osm_base:
                # An Overpass response with no `osm3s.timestamp_osm_base`. Real responses
                # normally carry it, which is exactly why the fallback path went unnoticed.
                payload = {key: value for key, value in payload.items() if key != "osm3s"}
            if overpass_omit_meta:
                # The same response with no per-element `meta`. This is the genuinely
                # unknowable case: neither the batch nor the members carry a time, so "we
                # cannot tell how old this is" is the only honest answer.
                payload = {
                    **payload,
                    "elements": [
                        {
                            key: value
                            for key, value in element.items()
                            if key not in {"timestamp", "version", "changeset", "user", "uid"}
                        }
                        for element in payload["elements"]
                    ],
                }
            return httpx.Response(200, json=payload)
        if host == "api.open-meteo.com":
            return httpx.Response(200, json=FORECAST_PAYLOAD)
        if host == "routing.openstreetmap.de":
            # FOSSGIS runs one instance per mode, so the path names the profile twice:
            # /routed-foot/table/v1/foot/... A real foot profile answers with foot data.
            if "/routed-foot/" in path:
                return httpx.Response(200, json=OSRM_FOOT_TABLE_PAYLOAD)
            return httpx.Response(200, json=OSRM_TABLE_PAYLOAD)
        if host == "router.project-osrm.org":
            # The demo server's defining flaw, reproduced faithfully: any profile name under
            # the single car instance returns the car matrix, with HTTP 200. A test that
            # mocks this server must therefore be able to prove the provider does not believe
            # a foot answer that is really a car answer.
            return httpx.Response(200, json=OSRM_TABLE_PAYLOAD)
        return httpx.Response(404, text=f"unexpected host {host}{path}")

    return handler


#: The two deployments, so a test states which one it is exercising instead of relying on
#: whichever happens to be the default. The default is FOSSGIS; the demo is kept because its
#: "foot returns car data" behaviour is the failure this provider exists to avoid, and it can
#: only be tested against a server that behaves that way.
CAR_ONLY_OSRM = "https://router.project-osrm.org"
FOSSGIS_OSRM = "https://routing.openstreetmap.de"


def build_stack(
    *,
    overpass_fail_first_mirror: bool = False,
    overpass_omit_osm_base: bool = False,
    overpass_omit_meta: bool = False,
    routing_base_url: str = FOSSGIS_OSRM,
    **provider_kwargs,
):
    http = FreeHttpClient(
        user_agent="TraverTest/1.0",
        **NO_RETRY,
        transport=httpx.MockTransport(
            _handler(
                overpass_fail_first_mirror=overpass_fail_first_mirror,
                overpass_omit_osm_base=overpass_omit_osm_base,
                overpass_omit_meta=overpass_omit_meta,
            )
        ),
    )
    geo = GeoResolver(http)
    poi = OverpassPoiProvider(http=http, geo_resolver=geo, **provider_kwargs)
    weather = OpenMeteoWeatherProvider(http=http, geo_resolver=geo)
    routing = OsmRoutingProvider(http=http, base_url=routing_base_url)
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


def test_capital_outranks_a_lesser_administrative_seat():
    """A national capital must beat a county seat even with a smaller rank gap.

    ``"PPLC"`` does not start with ``"PPLA"``, so testing ``startswith("PPLA")``
    for the top rank demoted every capital: Paris, France (PPLC) scored 1 while
    Paris, Texas (PPLA2) scored 2, and because the tuple compares rank before
    population the 24k-population town beat the 2.1M-population capital.
    """
    assert _feature_rank("PPLC") > _feature_rank("PPLA2")
    assert _feature_rank("PPLC") > _feature_rank("PPLA")
    assert _feature_rank("PPLA") > _feature_rank("PPLA2")
    assert _feature_rank("PPLA2") > _feature_rank("PPL")

    capital = {"feature_code": "PPLC", "population": 2138551}
    small_town = {"feature_code": "PPLA2", "population": 24782}
    assert _score_open_meteo_result(capital) > _score_open_meteo_result(small_town)


async def test_geocoder_prefers_a_capital_over_a_larger_ranked_namesake():
    """Regression: "巴黎" resolved to Paris, Kentucky, ~8,300 km from France."""
    payload = {
        "results": [
            {
                "id": 1,
                "name": "巴黎",
                "latitude": 48.85341,
                "longitude": 2.3488,
                "feature_code": "PPLC",
                "country": "法国",
                "country_code": "FR",
                "population": 2138551,
                "timezone": "Europe/Paris",
            },
            {
                "id": 2,
                "name": "巴黎",
                "latitude": 38.2098,
                "longitude": -84.253,
                "feature_code": "PPLA2",
                "country": "美国",
                "country_code": "US",
                "population": 9870,
                "timezone": "America/New_York",
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    geo = GeoResolver(http, sources=(SOURCE_OPEN_METEO,))
    place = await geo.resolve("巴黎")
    assert place.country_code == "FR"
    assert place.latitude == pytest.approx(48.85341)
    await http.aclose()


async def test_a_provisional_geocode_is_overridden_by_the_next_source():
    """Regression: searching 丽江 returned a namesake town 1,154 km away.

    Open-Meteo answers "丽江" with a single ``PPL`` that has no population, in the
    wrong province. Because that is a non-empty answer the old resolver accepted
    it and never consulted Nominatim, so the user got a coherent-looking plan for
    the wrong city. A provisional answer must not stop the search.
    """
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        seen_hosts.append(host)
        if host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 1,
                            "name": "丽江",
                            "latitude": 28.62428,
                            "longitude": 113.88887,
                            "feature_code": "PPL",
                            "country": "中国",
                            "country_code": "CN",
                            "population": None,
                            "timezone": "Asia/Shanghai",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "place_id": 1,
                    "osm_id": 99,
                    "lat": "26.86000",
                    "lon": "100.22500",
                    "name": "丽江市",
                    "display_name": "丽江市, 云南省, 中国",
                    "class": "boundary",
                    "type": "administrative",
                    "boundingbox": ["26.5", "27.2", "99.9", "100.6"],
                    "address": {"city": "丽江市", "state": "云南省", "country": "中国"},
                }
            ],
        )

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    geo = GeoResolver(http)
    place = await geo.resolve("丽江")

    assert "nominatim.openstreetmap.org" in seen_hosts, (
        "the provisional answer must be corroborated"
    )
    assert place.source_reference.source == "nominatim"
    assert place.latitude == pytest.approx(26.86)
    # The provisional coordinate would have been ~1,150 km east in Hunan.
    assert place.longitude == pytest.approx(100.225)
    await http.aclose()


async def test_a_confident_geocode_does_not_trigger_a_second_source():
    """Corroboration must not add a Nominatim call to the common case."""
    seen_hosts: list[str] = []
    base = _handler()

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(recording_handler))
    geo = GeoResolver(http)
    # 成都 is a PPLA with a real population, so it is trusted immediately.
    await geo.resolve("成都")
    assert "nominatim.openstreetmap.org" not in seen_hosts
    await http.aclose()


async def test_a_provisional_geocode_is_kept_when_nothing_better_exists():
    """A provisional answer still beats refusing to plan when the fallback is down."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 1,
                            "name": "某镇",
                            "latitude": 30.0,
                            "longitude": 120.0,
                            "feature_code": "PPL",
                            "country": "中国",
                            "country_code": "CN",
                            "population": None,
                            "timezone": "Asia/Shanghai",
                        }
                    ]
                },
            )
        return httpx.Response(503, text="unavailable")

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    geo = GeoResolver(http)
    place = await geo.resolve("某镇")
    assert place.latitude == pytest.approx(30.0)
    assert place.source_reference.source == "open-meteo-geocoding"
    await http.aclose()


async def test_nominatim_prefers_a_settlement_point_over_a_boundary_centroid():
    """Regression: 平遥 resolved to its county centroid ~11 km from the old town.

    A ``county`` result reports the centroid of the area, which fell outside the
    +/-0.045deg search box, so the destination came back with zero POIs even
    though OSM maps 平遥古城.

    The payload mirrors the live jsonv2 shape, which carries ``addresstype`` and
    **no** ``class`` field. An earlier version of this test keyed off ``class``
    and passed against a payload the service never actually returns.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "place_id": 1,
                    "osm_id": 1,
                    "lat": "37.1393929",
                    "lon": "112.2849966",
                    "name": "平遥县",
                    "display_name": "平遥县, 晋中市, 山西省, 中国",
                    "type": "administrative",
                    "addresstype": "county",
                    "boundingbox": ["37.0", "37.4", "112.0", "112.5"],
                    "address": {"county": "平遥县", "city": "晋中市", "country": "中国"},
                },
                {
                    "place_id": 2,
                    "osm_id": 2,
                    "lat": "37.2037904",
                    "lon": "112.1771043",
                    "name": "平遥县",
                    "display_name": "平遥县, 晋中市, 山西省, 中国",
                    "type": "city",
                    "addresstype": "city",
                    "boundingbox": ["37.15", "37.25", "112.12", "112.24"],
                    "address": {"county": "平遥县", "city": "晋中市", "country": "中国"},
                },
            ],
        )

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    geo = GeoResolver(http, sources=(SOURCE_NOMINATIM,))
    place = await geo.resolve("平遥")
    assert place.latitude == pytest.approx(37.2037904)
    assert place.longitude == pytest.approx(112.1771043)
    await http.aclose()


async def test_nominatim_will_not_jump_to_a_distant_namesake():
    """The settlement preference must stay local.

    Every geocode has a namesake somewhere, so "prefer the settlement type" has
    to be bounded or it would replace one wrong-city result with another.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "place_id": 1,
                    "osm_id": 1,
                    "lat": "30.0000",
                    "lon": "120.0000",
                    "name": "某县",
                    "display_name": "某县, 中国",
                    "type": "administrative",
                    "addresstype": "county",
                    "address": {"county": "某县", "country": "中国"},
                },
                {
                    # Same name, but on another continent: must be ignored.
                    "place_id": 2,
                    "osm_id": 2,
                    "lat": "48.0000",
                    "lon": "2.0000",
                    "name": "某县",
                    "display_name": "某县, 法国",
                    "type": "city",
                    "addresstype": "city",
                    "address": {"city": "某县", "country": "法国"},
                },
            ],
        )

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    geo = GeoResolver(http, sources=(SOURCE_NOMINATIM,))
    place = await geo.resolve("某县")
    assert place.latitude == pytest.approx(30.0)
    assert place.longitude == pytest.approx(120.0)
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
    # And the basis says this timestamp is OSM's, not ours. Without this assertion the test
    # above passes even when the provider mislabels the fallback as provider-reported.
    assert reference.observed_at_basis is ObservationBasis.PROVIDER_REPORTED
    assert "osm_base_missing" not in reference.request_context
    await http.aclose()


async def test_the_snapshot_time_is_not_the_age_of_the_elements():
    """The distinction that makes 时效性 true for OSM rather than merely asserted.

    ``osm3s.timestamp_osm_base`` is when OSM cut the database. It is genuinely
    provider-reported, so the freshness check on it passes — and it says nothing about the
    elements. Measured against the live service, 杭州西湖's snapshot was 1 minute old while the
    median element had last been edited 914 days earlier, and 19 of 46 were over five years old.

    So a plan built on snapshots taken this minute could pass every freshness check while
    listing restaurants that closed years ago. The fixture carries one element from 2019 and one
    from 2021 against a snapshot seconds old, so both halves are asserted at once.
    """
    http, _, poi, *_ = build_stack()
    hotels = (await poi.search_hotels(make_profile())).data
    reference = hotels[0].source_references[0]

    element_age = reference.element_age
    assert element_age is not None, (
        "the reference carries no element age, so the snapshot time is being reported as the "
        "age of the data"
    )
    assert element_age.count == 3, "all three mapped elements carry a `meta` timestamp"

    # The snapshot and the elements are two different magnitudes, and that gap is the point.
    #
    # Asserted as a *difference*, not as a bound and not as a ratio. The fixture's snapshot is a
    # fixed instant while "now" moves, and both figures here are measured back from "now" — so:
    #
    #   * `data_age_seconds < 3 * 86400` was only true for three days after the fixture's date,
    #     which is a test that cannot stay green. It asserted when the suite runs, not what the
    #     code does, and it went red at 3.09 days.
    #   * A ratio (`median > snapshot * 100`) decays too, toward 1, because numerator and
    #     denominator grow at the same rate. It read 571 at the time of writing and would have
    #     started failing about nine days later — a second expiry hiding behind the first.
    #   * The difference is what is actually fixed by the fixture: it is
    #     `snapshot_instant - element_edit_instant`, two constants, and it never moves.
    #
    # The claim being tested is that the reported data age is about our copy and not about the
    # facts in it, and a difference is the shape of that claim.
    snapshot_to_median = element_age.median_seconds - reference.data_age_seconds
    snapshot_to_oldest = element_age.oldest_seconds - reference.data_age_seconds

    # Tie the arithmetic to the fixture's own instants rather than to a magic number, so the
    # invariant is visible. Both sides are subtractions of two absolute times, and the seconds
    # of slop cover the integer truncation in each age.
    #
    # Conditional because `data_age_seconds` clamps at zero: with the clock ahead of the fixture
    # — the real case, and always — the two sides agree exactly, and that agreement is what shows
    # both numbers sit on one timeline. With the clock *behind* the fixture the snapshot reads as
    # "in the future", the age clamps to 0, and the sides differ by precisely that offset. That
    # needs a backwards clock, so it is not a case worth failing over; skipping is honest about
    # which direction this check covers, and the assertions below hold either way.
    if reference.observed_at <= reference.fetched_at:
        assert snapshot_to_oldest == pytest.approx(
            (reference.observed_at - element_age.oldest_at).total_seconds(), abs=2
        ), (
            "the oldest element's age is not measured against the same clock as the snapshot, so "
            "the two numbers cannot be subtracted"
        )
    else:
        assert snapshot_to_oldest == pytest.approx(
            (reference.fetched_at - element_age.oldest_at).total_seconds(), abs=2
        ), "the snapshot is ahead of our fetch, and the clamped age does not match either clock"

    assert snapshot_to_median > 4 * 365 * 86400, (
        "the median element must be years older than the snapshot we received; if the two are "
        "close, the snapshot is being reported as the age of the data"
    )
    assert element_age.median_seconds > 365 * 86400, (
        "the median element in this fixture is years old, and saying otherwise is the defect"
    )
    assert element_age.oldest_seconds > 5 * 365 * 86400

    # The oldest element is named by its own absolute time, so a reader can check the claim
    # rather than trusting a relative age that decays as the plan sits in the database.
    assert element_age.oldest_at.isoformat().startswith("2019-03-14")
    assert element_age.median_days > 365

    # And every element is older than the one-day horizon for a POI listing, which is the
    # finding the audit now raises.
    assert element_age.older_than_horizon == 3
    assert element_age.horizon_seconds == 86400
    # The members are OSM elements, which are *edited*. The report's wording follows this field,
    # and the statistic is shared with the review path — so a default that drifted, or a copy of
    # this call that named the wrong kind, would describe OSM records as though they had been
    # written once and never touched, which is false for a wiki.
    assert element_age.member_kind is ElementAgeKind.EDITED
    await http.aclose()


async def test_the_median_is_the_middle_element_and_not_an_average():
    """Which statistic it is, pinned to the actual middle element.

    A mean would let a bulk of current entries hide an ancient one, and that is the failure that
    sends a traveller to a closed shop: "44 current restaurants and one shut in 2011" and "45
    restaurants mapped last month" can share a mean. So the median is asserted against the
    fixture's own middle element — 2021-11-30, between the 2019 and 2026 ones — which no mean,
    minimum or maximum can also satisfy.
    """
    http, _, poi, *_ = build_stack()
    hotels = (await poi.search_hotels(make_profile())).data
    element_age = hotels[0].source_references[0].element_age
    assert element_age is not None

    middle = datetime(2021, 11, 30, 2, 15, 44, tzinfo=timezone.utc)
    expected_median = (element_age.computed_at - middle).total_seconds()
    assert abs(element_age.median_seconds - expected_median) < 2, (
        f"median is {element_age.median_seconds}s but the middle element is "
        f"{expected_median:.0f}s old — a mean, min or max would land elsewhere"
    )

    # And it is genuinely bracketed by the other two, so the fixture has a spread to detect.
    oldest = datetime(2019, 3, 14, 8, 21, 5, tzinfo=timezone.utc)
    newest = datetime(2026, 8, 2, 11, 0, 0, tzinfo=timezone.utc)
    assert (
        element_age.oldest_seconds
        > expected_median
        > (element_age.computed_at - newest).total_seconds()
    )
    assert element_age.oldest_seconds == int((element_age.computed_at - oldest).total_seconds())
    await http.aclose()


async def test_os_map_reports_no_element_age_when_the_response_carries_no_meta():
    """``None``, not zeros. Absent metadata is not a set of freshly edited elements.

    A request without ``meta`` — or an older mirror — returns no per-element timestamps. Zeros
    would be read as "everything was edited this minute", which is the exact claim this whole
    change exists to stop being made. The snapshot time is still reported, because it is still
    real.
    """
    http, _, poi, *_ = build_stack(overpass_omit_meta=True)
    hotels = (await poi.search_hotels(make_profile())).data
    reference = hotels[0].source_references[0]

    assert reference.element_age is None
    # The snapshot is still provider-reported and still reported: it is a true statement about
    # our copy, and dropping it would lose information rather than avoid a false one.
    assert reference.observed_at_basis is ObservationBasis.PROVIDER_REPORTED
    assert reference.observed_at.isoformat().startswith("2026-09-27T16:13:33")
    # And the context does not claim an age it does not have.
    assert "element_age_median_days" not in reference.request_context
    await http.aclose()


async def test_a_silent_osm_response_is_not_reported_as_a_provider_stamped_one():
    """The 时效性 rule applied to the one provider that had it backwards.

    Overpass normally publishes ``osm3s.timestamp_osm_base``. ``_osm_base_timestamp`` used to
    return ``datetime.now()`` when it did not, and the reference was unconditionally stamped
    ``PROVIDER_REPORTED`` — so an undated response produced a citation asserting that the data
    was seconds old **and that OSM had said so**.

    That is a fabricated observation time on the exact field the freshness system exists to
    protect, and it is worse than a missing one: ``age_is_about_the_data`` returns True, so the
    datum counts as evidence of recency, and ``audit`` never reports it as age-unknown.

    The correct shape already existed next door in ``amap/weather.py``: fall back to the fetch
    time for ``observed_at``, switch the basis to ``SOURCE_SILENT``, and record the absence.
    """
    http, _, poi, *_ = build_stack(overpass_omit_osm_base=True)
    hotels = (await poi.search_hotels(make_profile())).data
    assert hotels, "the fixture must still return data, or this tests nothing"

    reference = hotels[0].source_references[0]
    assert reference.observed_at_basis is ObservationBasis.SOURCE_SILENT, (
        "OSM published no snapshot time, so the timestamp is ours and must not be labelled "
        "as the source's own"
    )
    assert reference.request_context["osm_base_missing"] is True

    # The two consequences that make this matter, asserted directly rather than inferred.
    assert reference.observed_at_basis.age_is_about_the_data is False, (
        "a silent source must never be counted as evidence about the data's age"
    )
    # `observed_at` is still populated — it is the fetch time — so a reader always has a
    # timestamp; only its *meaning* changed.
    assert reference.observed_at is not None
    assert abs((reference.observed_at - reference.fetched_at).total_seconds()) < 5
    await http.aclose()


async def test_a_silent_osm_response_is_reported_as_age_unknown_by_the_audit():
    """The end-to-end consequence: the freshness audit must classify it as unverifiable.

    This is what the mislabelling hid. With ``PROVIDER_REPORTED`` and a timestamp of *now*, the
    audit saw a datum 0 seconds old and said nothing at all — the plan silently implied the OSM
    data was current. With ``SOURCE_SILENT`` it is reported as ``age_unknown``, which is the
    honest finding: we asked, and nobody told us when the data was made.

    Both the batch time **and** the per-element `meta` are withheld, because that is what
    "nobody told us" means. With meta present the audit now answers ``too_old`` instead —
    a more useful finding, and the subject of its own tests below.
    """
    from travel_api.services.freshness import FreshnessService

    http, _, poi, *_ = build_stack(overpass_omit_osm_base=True, overpass_omit_meta=True)
    hotels = (await poi.search_hotels(make_profile())).data
    references = hotels[0].source_references

    assert references[0].element_age is None, (
        "the fixture still carries meta, so this test would pass without exercising the "
        "genuinely-unknown path"
    )

    findings = FreshnessService().audit([("attraction", ref) for ref in references])
    assert findings, (
        "an undated OSM response produced no freshness finding, so the plan implies the data "
        "is current"
    )
    assert [finding["reason"] for finding in findings] == ["age_unknown"]
    finding = findings[0]
    assert finding["source"] == "openstreetmap"
    # The reader is told both times, so they can see which one the basis applies to.
    assert finding["observed_at"] and finding["fetched_at"]
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
    """Derived from today, because a hardcoded "far future" date stops being one.

    This read `date(2027, 6, 1)`, which was beyond the 16-day horizon when it was written and
    silently stops being so on **2027-05-16** — after which the provider correctly returns no
    warning and this test fails for a reason that has nothing to do with the code. Found by
    running the suite with the clock shifted (`scripts/clock_shift_plugin.py`).
    """
    http, _, _, weather, _ = build_stack()
    beyond = date.today() + timedelta(days=MAX_FORECAST_DAYS + 30)
    far_future = make_profile(start_date=beyond, end_date=beyond + timedelta(days=4))
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
    # The default deployment has a real foot profile, and 成都 is a relaxed city trip, so the
    # leg is routed on the pedestrian network — hence the foot distance, not the car one.
    assert forward.distance_meters == 3900
    assert forward.mode is TravelMode.WALK
    # Walking legs carry no fabricated fare.
    assert forward.cost is None
    await http.aclose()


async def test_walking_on_a_real_foot_profile_is_a_measurement_not_an_estimate():
    """The capability gain from pointing at a deployment that has a foot profile.

    A walking duration taken from the pedestrian network is an observation: OSRM measured
    how long this walk takes on the paths people actually walk. Marking it as an estimate
    would understate the data just as surely as presenting a car time as a walk would
    overstate it, so both directions are pinned here.
    """
    http, _, _, _, routing = build_stack(routing_base_url=FOSSGIS_OSRM)
    from uuid import uuid4

    first, second = uuid4(), uuid4()
    points = [
        RoutePoint(id=first, latitude=30.6558, longitude=104.0805),
        RoutePoint(id=second, latitude=30.6495, longitude=104.0432),
    ]
    result = await routing.calculate_routes(make_profile(), points)
    forward = next(leg for leg in result.data if leg.origin_id == first)

    # 3120 s is 52 minutes, and it is OSRM's own figure for the walk.
    assert forward.duration_minutes == 52
    assert forward.is_estimate is False
    assert forward.distance_meters == 3900

    context = forward.source_reference.request_context
    assert context["osrm_profile"] == "foot"
    assert context["duration_source"] == "osrm_foot_profile"
    assert context["duration_is_measured"] is True
    assert context["walking_speed_mps"] is None, (
        "no speed constant was used, so none may be reported"
    )
    await http.aclose()


async def test_walking_duration_is_derived_not_taken_from_car_routing():
    """A car-only deployment must not have its car time presented as a walk.

    The public OSRM demo answers a foot request with HTTP 200 and its car matrix — it does not
    fail, it answers confidently with the wrong mode. Its 426.6 s figure for this leg is
    ~40 km/h. So on this deployment the provider borrows only the *distance*, derives the
    duration from a walking speed, and flags it.
    """
    http, _, _, _, routing = build_stack(routing_base_url=CAR_ONLY_OSRM)
    from uuid import uuid4

    first, second = uuid4(), uuid4()
    points = [
        RoutePoint(id=first, latitude=30.6558, longitude=104.0805),
        RoutePoint(id=second, latitude=30.6495, longitude=104.0432),
    ]
    result = await routing.calculate_routes(make_profile(), points)
    forward = next(leg for leg in result.data if leg.origin_id == first)

    # 4818 m at 1.25 m/s is about 64 minutes, not the 7 minutes the car profile reported.
    assert forward.duration_minutes == 64
    assert forward.is_estimate is True

    # And the car figure must not have leaked in.
    assert forward.duration_minutes != 7

    reference = forward.source_reference
    assert reference.request_context["osrm_profile"] == "driving"
    assert reference.request_context["duration_source"] == "derived_walk_speed"
    # The reason the duration is derived is disclosed in the datum itself.
    assert reference.request_context["duration_is_measured"] is False
    assert reference.request_context["walking_speed_mps"] == 1.25
    await http.aclose()


async def test_a_deployment_without_the_profile_is_named_in_the_provenance():
    """Which server answered is part of the fact, not a debugging detail.

    Two deployments produce different walking numbers for the same leg, so a stored plan that
    did not record which one it came from could not be re-checked later.
    """
    http, _, _, _, fossgis = build_stack(routing_base_url=FOSSGIS_OSRM)
    from uuid import uuid4

    points = [
        RoutePoint(id=uuid4(), latitude=30.6558, longitude=104.0805),
        RoutePoint(id=uuid4(), latitude=30.6495, longitude=104.0432),
    ]
    result = await fossgis.calculate_routes(make_profile(), points)
    context = result.data[0].source_reference.request_context
    assert context["osrm_server"] == "routing.openstreetmap.de"
    assert context["osrm_server_label"]
    # A route is only as current as the road graph it was computed on, and this deployment
    # rebuilds it about every two days — so the age of the graph is recorded with the route.
    assert context["data_freshness"]
    assert sorted(context["profiles_honoured"]) == ["bike", "driving", "foot"]
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
    assert forward.source_reference.request_context["duration_source"] == "osrm_driving_profile"


async def test_routing_asks_for_the_profile_that_matches_the_mode():
    """On a deployment with a foot profile, each mode must ask for its own.

    The old code sent ``driving`` for every mode because that was all the demo server had.
    That is now wrong: it would throw away the foot profile and quietly downgrade a measured
    walk to a derived one.
    """
    seen: list[str] = []
    base = _handler()

    def recording_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "routing.openstreetmap.de":
            # /routed-foot/table/v1/foot/<coords>
            seen.append(request.url.path.split("/")[4])
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(recording_handler))
    routing = OsmRoutingProvider(http=http, base_url=FOSSGIS_OSRM)
    from uuid import uuid4

    from travel_api.domain.models import Pace

    points = [
        RoutePoint(id=uuid4(), latitude=30.6558, longitude=104.0805),
        RoutePoint(id=uuid4(), latitude=30.6495, longitude=104.0432),
    ]
    await routing.calculate_routes(make_profile(pace=Pace.RELAXED), points)
    await routing.calculate_routes(make_profile(pace=Pace.FULL), points)
    assert seen == ["foot", "driving"]
    await http.aclose()


async def test_routing_never_requests_an_unsupported_profile():
    """A car-only server must never be asked for a profile it would silently mis-serve."""
    seen_profiles: list[str] = []
    base = _handler()

    def recording_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "router.project-osrm.org":
            # /table/v1/<profile>/<coords>
            seen_profiles.append(request.url.path.split("/")[3])
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(recording_handler))
    routing = OsmRoutingProvider(http=http, base_url=CAR_ONLY_OSRM)
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


async def test_an_unknown_deployment_is_treated_as_car_only():
    """The pessimistic default, and the reason for it.

    A self-hosted OSRM may well have every profile, but we cannot verify that from here, and
    the cost of being wrong upward is a car duration labelled as a walk. An operator who knows
    their server supports more says so explicitly; until then walking is derived and flagged
    rather than presented as measured.
    """
    http = FreeHttpClient(
        user_agent="TraverTest/1.0",
        **NO_RETRY,
        transport=httpx.MockTransport(_handler()),
    )
    routing = OsmRoutingProvider(http=http, base_url="https://osrm.internal.example")
    assert sorted(routing.server.prefixes) == ["driving"]
    assert routing.serves_mode(TravelMode.WALK), "walking is still served, by distance proxy"
    assert routing.serves_mode(TravelMode.DRIVE)

    # And the operator can correct it.
    declared = OsmRoutingProvider(
        http=http,
        base_url="https://osrm.internal.example",
        honoured_profiles="driving,foot",
    )
    assert sorted(declared.server.prefixes) == ["driving", "foot"]
    await http.aclose()


async def test_transit_is_declined_rather_than_answered_with_a_car_time():
    """No public OSRM has a transit profile, so none may answer a transit request.

    The mode table used to map transit to ``driving``, which would have returned a 7-minute
    car trip under a 公交/地铁 label. 公交/地铁 is precisely what 高德 adds, so declining here
    is what makes the capability matrix's "公交/地铁：缺失" true.
    """
    http, _, _, _, routing = build_stack()
    assert not routing.serves_mode(TravelMode.TRANSIT)
    assert unserved_aspects_of(routing, "calculate_routes") == frozenset({"transit", "cycling"})
    await http.aclose()


async def test_the_provider_serves_every_aspect_it_claims_to():
    """Claim and behaviour compared, which is what the old coverage bug lacked.

    The false coverage in the catalogue existed because nothing checked a declaration against
    what the code actually produced. This walks the mode table and asserts that every aspect
    the provider does *not* declare unserved really does yield a leg, and that every aspect it
    does declare unserved really is declined.
    """
    http, _, _, _, routing = build_stack()
    unserved = unserved_aspects_of(routing, "calculate_routes")
    aspect_by_mode = {
        TravelMode.WALK: "walking",
        TravelMode.DRIVE: "driving",
        TravelMode.TAXI: "driving",
        TravelMode.TRANSIT: "transit",
    }
    from uuid import uuid4

    from travel_api.domain.models import Pace

    points = [
        RoutePoint(id=uuid4(), latitude=30.6558, longitude=104.0805),
        RoutePoint(id=uuid4(), latitude=30.6495, longitude=104.0432),
    ]
    for mode, aspect in aspect_by_mode.items():
        claimed_served = aspect not in unserved
        assert routing.serves_mode(mode) is claimed_served, (
            f"{mode.value} -> {aspect}: serves_mode says {routing.serves_mode(mode)} but the "
            f"declaration says served={claimed_served}"
        )

    # Spot-check the behaviour behind the claim for the mode a city trip actually uses.
    assert "walking" not in unserved
    walked = await routing.calculate_routes(make_profile(pace=Pace.RELAXED), points)
    assert walked.data, "walking is claimed as served but produced no legs"
    await http.aclose()


async def test_a_transit_request_makes_no_http_call_at_all():
    """Declining has to be a decision, not a failed request.

    The mode table check above proves ``serves_mode`` reports no transit profile. This
    proves the request never leaves the process: querying a car-only server and discarding
    the answer would waste a real request, and on the demo server it would return HTTP 200
    with byte-identical car numbers — one code path away from labelling a 7-minute drive
    as a bus ride.
    """
    from uuid import uuid4

    seen: list[str] = []
    base = _handler()

    def counting_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return base(request)

    http = FreeHttpClient(
        user_agent="TraverTest/1.0", **NO_RETRY, transport=httpx.MockTransport(counting_handler)
    )
    routing = OsmRoutingProvider(http=http, base_url=FOSSGIS_OSRM)
    points = [
        RoutePoint(id=uuid4(), latitude=30.6558, longitude=104.0805),
        RoutePoint(id=uuid4(), latitude=30.6495, longitude=104.0432),
    ]
    profile = make_profile(pace=Pace.BALANCED)
    profile.travel_mode = TravelMode.TRANSIT

    result = await routing.calculate_routes(profile, points)

    assert result.data == [], "a transit request must not be answered with car data"
    assert result.source_references == [], (
        "and it must not stamp a source reference either, which would claim a fetch happened"
    )
    assert seen == [], "declining must not cost a request"
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
    #
    # `meta` is required alongside `center`: without it OSM returns no per-element
    # `timestamp`, so the only age available is the database snapshot — which is minutes old
    # for a city whose elements are years old. Asserted here so dropping it fails loudly
    # rather than silently reverting to reporting fresh snapshots as fresh data.
    assert ".hotels out center meta 11;" in query
    assert ".food out center meta 22;" in query
    assert ".sights out center meta 33;" in query
    # `center` must come before `meta`. Both orders are accepted by Overpass in practice, but
    # `center` is what makes ways carry coordinates at all: reverse them and every way is
    # dropped by the parser, silently reducing the POI set to nodes.
    assert "out meta center" not in query
    json.dumps(query)  # must be serialisable as a form value
