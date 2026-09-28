"""A real plan, planned through a stubbed 高德, over real HTTP.

Everything else in the suite tests the AMap pieces in isolation or the catalogue over
HTTP. This is the one that answers the question that actually matters for the product:
**when a trip is planned by an owner who has a key, does 高德's data end up in the
itinerary, with its fetch time attached?**

A local HTTP server stands in for 高德, replaying responses whose shapes were taken
from the verified contract (see ``docs/architecture/amap-contract.md``). Going
through a real socket rather than a mock transport is deliberate: it exercises the
HTTP client, the query-parameter key injection, the rate limiter and the JSON
decoding, which is where a wrong parameter name or a swapped coordinate survives a
unit test and fails in production.

The trip is planned with the free stack **off**, so nothing here can pass by accident
on OSM data: every place, route and forecast in the result must have come from the
stub.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.domain.models import ObservationBasis, Pace, RoutePoint, TravelMode, TripProfile
from travel_api.main import create_app
from travel_api.providers.amap import (
    AMAP_KEY_CREDENTIAL,
    AMAP_PROVIDER_ID,
    AMAP_ROUTING_PROVIDER_NAME,
    AMAP_WEATHER_PROVIDER_NAME,
)
from travel_api.providers.amap.weather import AMAP_REPORT_TIME_TZ
from travel_api.services.owner_context import acting_as

#: Two well-separated Beijing points, so a route between them is unambiguous.
TIANANMEN = "116.397428,39.909230"
SUMMER_PALACE = "116.275510,39.999000"

STUB_KEY = "amap-stub-key"


def _geocode() -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": "1",
        "geocodes": [
            {
                "formatted_address": "北京市",
                "province": "北京市",
                "city": "北京市",
                "citycode": "010",
                "district": "",
                "adcode": "110000",
                "location": TIANANMEN,
                "level": "省",
            }
        ],
    }


def _places(keyword: str) -> dict:
    """POIs, with a type path and a telephone, as 高德 returns them."""
    specs = {
        "景点": [
            ("B000A8UIN8", "故宫博物院", "风景名胜;风景名胜;世界遗产", TIANANMEN),
            ("B000A8UIN9", "颐和园", "风景名胜;公园广场;公园", SUMMER_PALACE),
        ],
        "美食": [("B0FFH1", "全聚德(前门店)", "餐饮服务;中餐厅;中餐厅", TIANANMEN)],
        "酒店": [("B0FFF1", "北京饭店", "住宿服务;宾馆酒店;五星级宾馆", TIANANMEN)],
    }
    pois = []
    for poi_id, name, poi_type, location in specs.get(keyword, []):
        pois.append(
            {
                "id": poi_id,
                "name": name,
                "type": poi_type,
                "tel": "010-65120000",
                "location": location,
                "address": f"北京市东城区{name}",
                "businessarea": "东城区",
            }
        )
    return {"status": "1", "info": "OK", "infocode": "10000", "count": str(len(pois)), "pois": pois}


def _distance(query: dict) -> dict:
    """The one-to-many sweep. ``origin_id`` is the 1-based index, as 高德 sends it."""
    origins = query.get("origins", [""])[0].split("|")
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "results": [
            {
                "origin_id": str(index),
                "dest_id": "1",
                "distance": "5200",
                "duration": "780",
            }
            for index in range(1, len(origins) + 1)
        ],
    }


def _transit() -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "route": {
            "origin": TIANANMEN,
            "destination": SUMMER_PALACE,
            "taxi_cost": "58",
            "transits": [
                {
                    "distance": "18400",
                    "duration": "3300",
                    "walking_distance": "900",
                    "cost": {"transit_fee": "6.00", "duration": "3300"},
                    "segments": [
                        {"walking": {"distance": "400", "duration": "320"}},
                        {
                            "bus": {
                                "buslines": [
                                    {
                                        "name": "地铁4号线大兴线(天宫院-安河桥北)",
                                        "id": "9000001",
                                        "departure_stop": "西单",
                                        "arrival_stop": "北宫门",
                                    }
                                ]
                            }
                        },
                    ],
                }
            ],
        },
    }


def _walking() -> dict:
    """A walking path. Deliberately carries no ``duration``.

    高德's walking response has no duration field at all, so the provider derives one
    from a stated speed and must mark it an estimate. Reproducing that shape here is
    what makes the estimate flag meaningful rather than assumed.
    """
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "route": {
            "origin": TIANANMEN,
            "destination": SUMMER_PALACE,
            "paths": [
                {
                    "distance": "410",
                    "steps": [{"instruction": "向东步行 410 米", "distance": "410"}],
                }
            ],
        },
    }


#: AMap's default publication time in the stub, in Beijing time, zone unstated — the
#: shape the real endpoint sends.
DEFAULT_REPORT_TIME = "2025-05-01 08:00:00"


def _weather(report_time: str = DEFAULT_REPORT_TIME) -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": "1",
        "forecasts": [
            {
                "city": "北京市",
                "adcode": "110000",
                "province": "北京",
                "reporttime": report_time,
                "casts": [
                    {
                        "date": "2025-05-01",
                        "week": "4",
                        "dayweather": "晴",
                        "nightweather": "多云",
                        "daytemp": "26",
                        "nighttemp": "14",
                        "daywind": "南",
                        "nightwind": "北",
                        "daypower": "1-3",
                        "nightpower": "1-3",
                    },
                    {
                        "date": "2025-05-02",
                        "week": "5",
                        "dayweather": "小雨",
                        "nightweather": "小雨",
                        "daytemp": "21",
                        "nighttemp": "13",
                        "daywind": "东",
                        "nightwind": "东",
                        "daypower": "1-3",
                        "nightpower": "1-3",
                    },
                ],
            }
        ],
    }


class _AmapStubHandler(BaseHTTPRequestHandler):
    """Serves the recorded 高德 shapes and records every request it received."""

    received: list[tuple[str, dict]] = []
    #: Overridable per test, so freshness behaviour can be exercised with a forecast
    #: published minutes ago versus days ago without depending on the wall clock.
    weather_report_time: str = DEFAULT_REPORT_TIME

    def do_GET(self) -> None:  # noqa: N802 - http.server's required name
        parsed = urlparse(self.path)
        query = {key: values for key, values in parse_qs(parsed.query).items()}
        type(self).received.append((parsed.path, query))

        path = parsed.path
        if path == "/v3/geocode/geo":
            payload = _geocode()
        elif path == "/v3/place/text":
            payload = _places(query.get("keywords", [""])[0])
        elif path == "/v3/distance":
            payload = _distance(query)
        elif path == "/v5/direction/transit/integrated":
            payload = _transit()
        elif path == "/v5/direction/walking":
            payload = _walking()
        elif path == "/v3/weather/weatherInfo":
            payload = _weather(type(self).weather_report_time)
        else:
            payload = {"status": "0", "info": "UNKNOWN_PATH", "infocode": "20000"}

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ARG002 - silence the stub
        return


@pytest.fixture
def amap_stub():
    _AmapStubHandler.received = []
    # Reset so an override in one test cannot leak into the next, which would make a
    # freshness assertion pass for the wrong reason.
    _AmapStubHandler.weather_report_time = DEFAULT_REPORT_TIME
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AmapStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _AmapStubHandler.received
    finally:
        _AmapStubHandler.weather_report_time = DEFAULT_REPORT_TIME
        server.shutdown()
        server.server_close()


@pytest.fixture
def app_client(tmp_path, amap_stub):
    base_url, received = amap_stub
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'plan.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        # Off, so nothing in the result can have come from the free sources.
        free_providers_enabled=False,
        mock_provider_enabled=False,
        amap_base_url=base_url,
        amap_ttl_seconds=300,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    with TestClient(create_app(settings)) as client:
        client.received = received  # type: ignore[attr-defined]
        yield client


def _profile(pace: Pace = Pace.BALANCED) -> TripProfile:
    return TripProfile(
        destination="北京",
        start_date=date(2025, 5, 1),
        end_date=date(2025, 5, 3),
        travelers=2,
        total_budget=6000,
        pace=pace,
    )


@pytest.mark.asyncio
async def test_a_plan_uses_amap_places_and_routes(app_client):
    """The whole chain, with the key stored the way a user stores it."""
    key, _ = app_client.app.state.auth_service.mint("owner-a", label="a")
    stored = app_client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": STUB_KEY},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert stored.status_code == 200

    with acting_as("owner-a"):
        preview = await app_client.app.state.trip_service.preview(_profile())

    # 高德's POIs are in the plan, which with the free stack off can only happen if the
    # credentialed provider was really built and used.
    names = {item.canonical_name for item in preview.attractions}
    assert "故宫博物院" in names, f"expected 高德 POIs, got {names}"
    assert preview.hotels, "the stub's hotel should have been used"

    paths = {path for path, _ in app_client.received}
    assert "/v3/place/text" in paths
    assert "/v3/geocode/geo" in paths or "/v3/weather/weatherInfo" in paths

    # The key really travelled as a query parameter, which is how 高德 is authenticated.
    for _path, query in app_client.received:
        assert query.get("key") == [STUB_KEY]


@pytest.mark.asyncio
async def test_every_route_leg_carries_its_fetch_time_and_no_caching_claim(app_client):
    """时效性 and the licence rule, asserted on the data rather than in prose.

    Every leg must state when it was fetched and that it was not cached, because
    presenting a cached duration as current is the misrepresentation the whole
    freshness design exists to prevent.
    """
    key, _ = app_client.app.state.auth_service.mint("owner-a", label="a")
    app_client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": STUB_KEY},
        headers={"Authorization": f"Bearer {key}"},
    )

    with acting_as("owner-a"):
        preview = await app_client.app.state.trip_service.preview(_profile(Pace.FULL))

    assert preview.route_legs, "driving legs should have been produced from the stub"
    for leg in preview.route_legs:
        reference = leg.source_reference
        assert reference.fetched_at is not None
        assert reference.source == AMAP_ROUTING_PROVIDER_NAME
        assert reference.request_context["cached"] is False
        # Driving is swept destination-by-destination, so it covers every pair.
        assert reference.request_context["coverage"] == "full_matrix"
        assert reference.request_context["duration_source"] == "amap_measured"

    # The measured durations must be 高德's, not a derived guess: 780 s = 13 minutes.
    assert {leg.duration_minutes for leg in preview.route_legs} == {13}
    assert all(leg.is_estimate is False for leg in preview.route_legs)


@pytest.mark.asyncio
async def test_a_transit_trip_gets_real_subway_lines_in_the_leg_context(app_client):
    """公交/地铁 is where 高德 earns its place, so its output is checked in detail.

    The line names come back attached to the leg, so the UI can show "地铁4号线"
    without a second request — and a reader can see the route was real rather than
    an estimate. The fare comes from 高德's own ``cost.transit_fee``, not from a
    per-kilometre guess.
    """
    key, _ = app_client.app.state.auth_service.mint("owner-a", label="a")
    app_client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": STUB_KEY},
        headers={"Authorization": f"Bearer {key}"},
    )

    # ``mode_for_pace`` selects driving, so transit is exercised through the very
    # provider object the registry built for this owner, rather than by forcing the
    # planner into a mode it would not choose on its own.
    registry = app_client.app.state.provider_registry.for_owner("owner-a")
    provider = next(
        item
        for item in registry.providers_for("calculate_routes")
        if item.name == AMAP_ROUTING_PROVIDER_NAME
    )

    route_points = [
        RoutePoint(id=uuid4(), latitude=39.909230, longitude=116.397428),
        RoutePoint(id=uuid4(), latitude=39.999000, longitude=116.275510),
    ]
    with acting_as("owner-a"):
        reference = provider._source_reference(
            mode=TravelMode.TRANSIT, point_count=len(route_points), destination="北京"
        )
        legs = await provider._transit_legs(route_points, _profile(), reference)

    assert legs, "the stub's transit plan should have produced a leg"
    leg = legs[0]
    context = leg.source_reference.request_context
    assert "地铁4号线大兴线(天宫院-安河桥北)" in context["transit_lines"]
    # Consecutive pairs, not a full matrix: transit plans are point-to-point.
    assert context["coverage"] == "consecutive_pairs"
    assert leg.mode is TravelMode.TRANSIT
    # The fare is 高德's own transit_fee of 6.00, kept as an exact value.
    assert leg.cost.value == Decimal("6.00")
    assert leg.cost.is_estimate is False
    # 3300 s from the stub's duration, not derived from the walking speed.
    assert leg.duration_minutes == 55
    assert leg.is_estimate is False


@pytest.mark.asyncio
async def test_the_weather_forecast_carries_amaps_own_report_time(app_client):
    """Two different times must both survive: when we asked, and how old 高德's
    observation is. Collapsing them would hide a stale forecast behind a fresh
    fetch."""
    key, _ = app_client.app.state.auth_service.mint("owner-a", label="a")
    app_client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": STUB_KEY},
        headers={"Authorization": f"Bearer {key}"},
    )

    registry = app_client.app.state.provider_registry.for_owner("owner-a")
    provider = next(
        item
        for item in registry.providers_for("search_weather")
        if item.name == AMAP_WEATHER_PROVIDER_NAME
    )

    with acting_as("owner-a"):
        result = await provider.search_weather(_profile())

    assert result.data, "the stub's forecast should have been parsed"
    forecast = result.data[0]
    assert forecast.location_name == "北京市"
    assert [day.day_date for day in forecast.days] == [date(2025, 5, 1), date(2025, 5, 2)]

    reference = forecast.source_references[0]
    assert reference.request_context["amap_report_time"] == "2025-05-01 08:00:00"
    assert reference.fetched_at is not None
    # AMap reports no precipitation, so the field must not claim 0 mm as a measurement.
    assert reference.request_context["absent_fields"].startswith("precipitation_mm")

    # 晴 maps to a real WMO code; 小雨 must be flagged as poor for outdoor plans.
    sunny, rainy = forecast.days
    assert sunny.weather_code == 0
    assert sunny.outdoor_friendly is True
    assert rainy.weather_code == 61
    assert rainy.outdoor_friendly is False
    # The adcode came from the stub's geocode response, which the weather endpoint
    # requires — it does not accept a city name.
    assert reference.request_context["adcode"] == "110000"


@pytest.mark.asyncio
async def test_the_forecast_observation_time_is_amaps_not_ours(app_client):
    """The honesty fix this round is about.

    AMap tells us when it published the forecast. ``observed_at`` must be *that* time,
    not the moment we asked, or a six-hour-old forecast reads as "observed just now"
    and a reader has no way to tell. The two times must therefore differ here, and the
    difference must be the publication lag rather than zero.
    """
    key, _ = app_client.app.state.auth_service.mint("owner-a", label="a")
    app_client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": STUB_KEY},
        headers={"Authorization": f"Bearer {key}"},
    )
    # Published ten minutes ago, in Beijing time, exactly as AMap would send it.
    published = datetime.now(AMAP_REPORT_TIME_TZ) - timedelta(minutes=10)
    _AmapStubHandler.weather_report_time = published.strftime("%Y-%m-%d %H:%M:%S")

    registry = app_client.app.state.provider_registry.for_owner("owner-a")
    provider = next(
        item
        for item in registry.providers_for("search_weather")
        if item.name == AMAP_WEATHER_PROVIDER_NAME
    )
    with acting_as("owner-a"):
        result = await provider.search_weather(_profile())

    reference = result.data[0].source_references[0]
    assert reference.observed_at_basis is ObservationBasis.PROVIDER_REPORTED
    assert reference.observed_at_basis.age_is_about_the_data is True

    # observed_at is the publication time; fetched_at is now. They are different facts
    # and must not have been collapsed into one value.
    assert reference.observed_at < reference.fetched_at
    assert 500 <= reference.data_age_seconds <= 900, (
        f"expected roughly ten minutes of publication lag, got {reference.data_age_seconds}s"
    )
    # Correct timezone handling: read as UTC it would sit eight hours in the future,
    # which would report an age of zero and hide a stale forecast.
    assert reference.data_age_seconds > 0


@pytest.mark.asyncio
async def test_a_stale_forecast_is_reported_rather_than_presented_as_current(app_client):
    """The "近期数据" rule, applied end to end.

    A forecast published three days ago is worthless for planning a future trip: it
    will describe the wrong weather with full confidence. The plan must carry that as
    a finding, not bury it — an empty ``stale_facts`` is the claim "everything here is
    within its horizon", which has to be earned.
    """
    key, _ = app_client.app.state.auth_service.mint("owner-a", label="a")
    app_client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": STUB_KEY},
        headers={"Authorization": f"Bearer {key}"},
    )
    old = datetime.now(AMAP_REPORT_TIME_TZ) - timedelta(days=3)
    _AmapStubHandler.weather_report_time = old.strftime("%Y-%m-%d %H:%M:%S")

    with acting_as("owner-a"):
        preview = await app_client.app.state.trip_service.preview(_profile())

    assert preview.stale_facts, "a three-day-old forecast must not pass silently"
    finding = next(item for item in preview.stale_facts if item["data_type"] == "weather")
    assert finding["reason"] == "too_old"
    assert finding["data_age_seconds"] > finding["max_age_seconds"]
    assert finding["source"] == AMAP_WEATHER_PROVIDER_NAME
    # The two times are both reported, so the reader can see what was fetched when,
    # and when the verdict itself was reached.
    assert finding["observed_at"] and finding["fetched_at"]
    assert finding["evaluated_at"]
    assert finding["observed_at"] in finding["detail"]


@pytest.mark.asyncio
async def test_a_recent_forecast_produces_no_staleness_claim(app_client):
    """The negative case, so the check cannot be a permanent false alarm.

    A rule that always fires is as useless as one that never does. A forecast
    published minutes ago must leave ``stale_facts`` empty — otherwise every plan
    would carry a warning and readers would learn to ignore it.
    """
    key, _ = app_client.app.state.auth_service.mint("owner-a", label="a")
    app_client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": STUB_KEY},
        headers={"Authorization": f"Bearer {key}"},
    )
    fresh = datetime.now(AMAP_REPORT_TIME_TZ) - timedelta(minutes=2)
    _AmapStubHandler.weather_report_time = fresh.strftime("%Y-%m-%d %H:%M:%S")

    with acting_as("owner-a"):
        preview = await app_client.app.state.trip_service.preview(_profile())

    assert [item for item in preview.stale_facts if item["data_type"] == "weather"] == []


@pytest.mark.asyncio
async def test_without_a_key_no_amap_call_is_made_at_all(app_client):
    """The zero-credential path must not touch 高德.

    Checked by counting the stub's requests: an unconfigured owner must produce no
    traffic, which is both the privacy expectation and what keeps a deployment
    without keys from spending anyone's quota.
    """
    with acting_as("owner-b"):
        preview = await app_client.app.state.trip_service.preview(_profile())

    assert app_client.received == [], f"unexpected 高德 calls: {app_client.received}"
    # And the trip is still produced, by the local planner.
    assert preview is not None
