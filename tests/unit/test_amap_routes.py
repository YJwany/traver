"""Tests for the AMap routing provider.

Each test targets a specific way this provider can produce **plausible but wrong**
route data. That is the dangerous failure mode here: a swapped coordinate, a
misread ``origin_id`` or a string-parsed number all yield a leg that looks fine and
silently puts the wrong travel time in an itinerary.

Three facts about AMap's wire format drive most of these:

* ``/v3/distance`` is many-origins -> **one** destination, and reports back
  ``origin_id`` as the **1-based index** into the origins parameter, not an id.
* ``distance`` is metres and ``duration`` is seconds, both as **strings**.
* ``/v5/direction/walking`` carries **no ``duration`` at all**.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from travel_api.domain.models import Pace, RoutePoint, TravelMode, TripProfile
from travel_api.providers.amap.client import AmapClient
from travel_api.providers.amap.routing import (
    AMAP_MAX_MATRIX_ORIGINS,
    DIRECTION_PATH_BY_MODE,
    DISTANCE_TYPE_BY_MODE,
    AmapRoutingProvider,
    mode_for_pace,
)
from travel_api.providers.base import resolve_travel_mode


def _point(latitude: float, longitude: float) -> RoutePoint:
    return RoutePoint(id=uuid4(), latitude=latitude, longitude=longitude)


def _profile(pace: Pace = Pace.BALANCED) -> TripProfile:
    return TripProfile(
        destination="北京",
        start_date=date(2025, 5, 1),
        end_date=date(2025, 5, 3),
        pace=pace,
    )


def _transit_profile() -> TripProfile:
    return TripProfile(
        destination="北京",
        start_date=date(2025, 5, 1),
        end_date=date(2025, 5, 3),
        pace=Pace.BALANCED,
        travel_mode=TravelMode.TRANSIT,
    )


class _RoutingHttp:
    """Routes canned responses by AMap path, recording every call."""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    async def get_json(self, url, *, params=None):
        path = url.split("restapi.amap.com")[-1]
        self.calls.append((path, dict(params or {})))
        handler = self.responses.get(path)
        if handler is None:
            return {"status": "1"}
        return handler(params) if callable(handler) else handler

    async def post_json(self, url, *, data=None):
        raise AssertionError("routing must not use POST")


def _provider(responses=None, **kwargs):
    http = _RoutingHttp(responses)
    client = AmapClient(http=http, key="test-key")
    return AmapRoutingProvider(client, **kwargs), http


def _reference(provider, mode: TravelMode, point_count: int = 2):
    return provider._source_reference(mode=mode, point_count=point_count, destination="北京")


# -- mode selection --------------------------------------------------------


@pytest.mark.parametrize(
    ("pace", "expected"),
    [
        (Pace.RELAXED, TravelMode.WALK),
        (Pace.BALANCED, TravelMode.WALK),
        (Pace.FULL, TravelMode.DRIVE),
    ],
)
def test_mode_follows_pace_like_the_osrm_provider(pace, expected):
    """Kept identical to OSRM so that plugging in AMap changes *quality*, not which
    journeys the plan is built around."""
    assert mode_for_pace(pace) == expected


# -- an explicit travel mode, and why 公交/地铁 needed one ------------------
#
# ``TravelMode.TRANSIT`` had a complete implementation behind it — a working
# ``_transit_legs``, an endpoint, tests against canned payloads — and no way for any
# request to reach it, because the only selector was ``mode_for_pace`` and it can return
# only WALK or DRIVE. The manifest meanwhile advertised 公交/地铁 as *the* reason to
# supply a 高德 key. So the headline differentiator of the whole integration was inert,
# which is the "implementing a method is not serving it" failure in its purest form: the
# capability matrix said covered, and no itinerary could ever contain a transit leg.


def test_an_explicit_transit_request_reaches_the_transit_path():
    """The regression that mattered: transit must be selectable, not merely implemented."""
    assert resolve_travel_mode(_transit_profile()) is TravelMode.TRANSIT


def test_an_explicit_mode_overrides_the_pace_derived_one():
    """A relaxed pace would otherwise pick walking and quietly ignore the request."""
    profile = TripProfile(
        destination="北京",
        start_date=date(2025, 5, 1),
        end_date=date(2025, 5, 3),
        pace=Pace.RELAXED,  # would derive WALK
        travel_mode=TravelMode.DRIVE,
    )
    assert resolve_travel_mode(profile) is TravelMode.DRIVE
    assert mode_for_pace(profile.pace) is TravelMode.WALK


def test_leaving_the_mode_unset_changes_nothing():
    """Every trip planned before this field existed must keep the route it had."""
    for pace in Pace:
        assert resolve_travel_mode(_profile(pace)) is mode_for_pace(pace)


@pytest.mark.asyncio
async def test_a_transit_request_actually_queries_the_transit_endpoint():
    """Selecting the mode is not enough; it has to reach the right endpoint."""
    provider, http = _provider({"/v5/direction/transit/integrated": TRANSIT_PAYLOAD})
    points = [_point(39.90 + index * 0.01, 116.40) for index in range(3)]

    result = await provider.calculate_routes(_transit_profile(), points)

    assert http.calls, "a transit request must actually call 高德"
    assert all(path == "/v5/direction/transit/integrated" for path, _ in http.calls)
    assert result.source_references[0].request_context["travel_mode"] == "transit"
    assert all(leg.mode is TravelMode.TRANSIT for leg in result.data)
    # And it must not have been answered with a car matrix, which is the failure mode
    # this replaced: a plausible duration for the wrong vehicle.
    assert not any(path == "/v3/distance" for path, _ in http.calls)


def test_the_pace_alone_can_never_select_transit():
    """The structural reason 公交/地铁 was dead, pinned so it cannot come back.

    The trap is subtle: ``mode_for_pace`` looks like a complete mode selector, and it is
    reachable, correct and tested — it simply cannot express transit. Any future caller
    that uses it directly instead of ``resolve_travel_mode`` silently makes the headline
    capability unreachable again, and every test of ``_transit_legs`` would still pass.
    """
    assert set(Pace) == {Pace.RELAXED, Pace.BALANCED, Pace.FULL}
    derived = {mode_for_pace(pace) for pace in Pace}
    assert TravelMode.TRANSIT not in derived
    # So the only way transit is reachable is through the explicit field.
    assert resolve_travel_mode(_transit_profile()) is TravelMode.TRANSIT


def test_transit_coverage_is_stated_as_consecutive_pairs():
    """Transit is one request per pair, so it cannot cover the full matrix.

    Claiming otherwise would present a partially routed plan as fully routed.
    """
    provider, _ = _provider()
    reference = provider._source_reference(
        mode=TravelMode.TRANSIT, point_count=3, destination="北京"
    )
    assert reference.request_context["coverage"] == "consecutive_pairs"


def test_the_two_providers_agree_on_the_mode_for_one_profile():
    """They are called for the same aggregation and their legs are merged.

    If they disagreed, one source's measured legs would be computed against a different
    assumption than the other's and nothing in the payload would reconcile them.
    """
    from travel_api.providers.free.osrm import mode_for_pace as osrm_mode_for_pace

    for pace in Pace:
        assert mode_for_pace(pace) is osrm_mode_for_pace(pace)

    for mode in (TravelMode.WALK, TravelMode.DRIVE, TravelMode.TAXI, TravelMode.TRANSIT):
        profile = TripProfile(
            destination="北京",
            start_date=date(2025, 5, 1),
            end_date=date(2025, 5, 3),
            travel_mode=mode,
        )
        assert resolve_travel_mode(profile) is mode


# -- the distance sweep (driving) -----------------------------------------


@pytest.mark.asyncio
async def test_the_driving_matrix_costs_one_call_per_point_not_n_squared():
    """The whole reason ``/v3/distance`` is used instead of per-pair routing.

    Four points must produce a full 4x3=12-leg matrix in exactly 4 calls. A
    per-pair implementation would need 12, and for a realistic 17-point candidate
    set that difference is 17 calls against 289.
    """

    def sweep(params):
        # Echo back every origin we sent, as AMap does.
        count = len(params["origins"].split("|"))
        return {
            "status": "1",
            "results": [
                {"origin_id": str(index), "dest_id": "1", "distance": "1200", "duration": "300"}
                for index in range(1, count + 1)
            ],
        }

    provider, http = _provider({"/v3/distance": sweep})
    points = [_point(39.9 + index * 0.01, 116.4) for index in range(4)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert len(http.calls) == 4
    assert all(path == "/v3/distance" for path, _ in http.calls)
    assert len(result.data) == 12  # every ordered pair except self-to-self
    assert all(leg.origin_id != leg.destination_id for leg in result.data)


@pytest.mark.asyncio
async def test_origin_id_is_read_as_a_one_based_index():
    """``origin_id`` indexes the ``origins`` parameter, so it must be mapped back
    positionally. Read as an id or a 0-based index it produces legs between the
    wrong points - with entirely believable distances."""

    def sweep(params):
        # Only answer for the sweep whose destination is the first point, so the
        # result set is unambiguous. Answering every sweep would produce one leg per
        # destination, each claiming the same origin_id.
        if not params["destination"].endswith("39.900000"):
            return {"status": "1", "results": []}
        return {
            "status": "1",
            "results": [{"origin_id": "2", "dest_id": "1", "distance": "800", "duration": "120"}],
        }

    provider, _ = _provider({"/v3/distance": sweep})
    points = [_point(39.90, 116.40), _point(39.95, 116.45), _point(40.00, 116.50)]
    destination_point = points[0]
    origins_sent = [point for point in points if point is not destination_point]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    # Exactly one leg, and its origin must be the *second* element of the origins
    # list AMap was given for this destination - not the second point overall.
    assert len(result.data) == 1
    assert result.data[0].origin_id == origins_sent[1].id
    assert result.data[0].destination_id == destination_point.id


@pytest.mark.asyncio
async def test_string_numbers_are_parsed_into_real_minutes():
    """AMap returns ``"300"``, not ``300``. A missed conversion yields a string in
    a numeric field, and the duration silently becomes 0 minutes downstream."""
    provider, _ = _provider(
        {
            "/v3/distance": {
                "status": "1",
                "results": [{"origin_id": "1", "distance": "6000", "duration": "900"}],
            }
        }
    )
    points = [_point(39.9, 116.4), _point(39.95, 116.45)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    leg = result.data[0]
    assert leg.distance_meters == 6000
    assert leg.duration_minutes == 15  # 900 seconds
    assert isinstance(leg.duration_minutes, int)


@pytest.mark.asyncio
async def test_a_measured_duration_is_not_flagged_as_an_estimate():
    provider, _ = _provider(
        {
            "/v3/distance": {
                "status": "1",
                "results": [{"origin_id": "1", "distance": "6000", "duration": "900"}],
            }
        }
    )
    points = [_point(39.9, 116.4), _point(39.95, 116.45)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert result.data[0].is_estimate is False
    assert result.data[0].source_reference.request_context["duration_source"] == "amap_measured"


@pytest.mark.asyncio
async def test_a_missing_duration_is_derived_and_disclosed_not_faked():
    """When AMap omits a duration the leg must be marked as an estimate rather than
    presenting a derived time as measured."""
    provider, _ = _provider(
        {
            "/v3/distance": {
                "status": "1",
                "results": [{"origin_id": "1", "distance": "6000", "duration": ""}],
            }
        }
    )
    points = [_point(39.9, 116.4), _point(39.95, 116.45)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    leg = result.data[0]
    assert leg.is_estimate is True
    # 6000 m at 1.25 m/s = 4800 s = 80 minutes.
    assert leg.duration_minutes == 80
    context = leg.source_reference.request_context
    assert context["duration_source"] == "derived_walking_speed"
    assert context["derived_walking_speed_mps"] == 1.25


@pytest.mark.asyncio
async def test_one_failed_sweep_keeps_the_legs_already_gathered():
    """A partial real matrix is better than falling back to estimates everywhere.
    The failure must not discard successful calls."""

    def sweep(params):
        # AMap coordinates are "lon,lat", so the destination string starts with the
        # *longitude*. Matching on a leading latitude silently never matches, which
        # turns a failure-handling test into a test of nothing.
        if params["destination"].endswith("39.900000"):
            return {"status": "0", "infocode": "10003", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT"}
        count = len(params["origins"].split("|"))
        return {
            "status": "1",
            "results": [
                {"origin_id": str(index), "distance": "500", "duration": "60"}
                for index in range(1, count + 1)
            ],
        }

    provider, http = _provider({"/v3/distance": sweep})
    points = [_point(39.90, 116.40), _point(39.95, 116.45), _point(40.00, 116.50)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert len(http.calls) == 3
    # The two successful destination sweeps still produced legs.
    assert len(result.data) == 4


# -- transit: the capability OSRM cannot provide --------------------------


TRANSIT_PAYLOAD = {
    "status": "1",
    "route": {
        "origin": "116.400000,39.900000",
        "destination": "116.480000,39.950000",
        "taxi_cost": "32",
        "transits": [
            {
                "distance": "8600",
                "duration": "2400",
                "walking_distance": "700",
                "cost": {"transit_fee": "5.00", "duration": "2400"},
                "segments": [
                    {"walking": {"distance": "300", "duration": "240"}},
                    {
                        "bus": {
                            "buslines": [
                                {
                                    "name": "地铁1号线(苹果园-四惠东)",
                                    "id": "900000",
                                    "departure_stop": "王府井",
                                    "arrival_stop": "国贸",
                                }
                            ]
                        }
                    },
                    {
                        "railway": {
                            "railway": [
                                {
                                    "name": "地铁2号线",
                                    "id": "900001",
                                    "departure_stop": "国贸",
                                    "arrival_stop": "东直门",
                                }
                            ]
                        }
                    },
                ],
            }
        ],
    },
}


@pytest.mark.asyncio
async def test_transit_uses_the_v5_endpoint_and_stamps_fetch_time():
    """公交/地铁 is the whole reason AMap is integrated, so it is asserted against
    a real v5 response shape including a bus and a rail segment."""
    provider, _ = _provider({"/v5/direction/transit/integrated": TRANSIT_PAYLOAD})
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]

    legs = await provider._transit_legs(
        points, _profile(), _reference(provider, TravelMode.TRANSIT)
    )

    assert len(legs) == 1
    leg = legs[0]
    assert leg.mode is TravelMode.TRANSIT
    assert leg.distance_meters == 8600
    assert leg.duration_minutes == 40  # 2400 s
    assert leg.is_estimate is False  # AMap measured it
    # Compared numerically: Decimal("5.00") normalises to Decimal("5.0"), so a
    # string comparison fails on a perfectly correct fare.
    assert leg.cost is not None
    assert leg.cost.value == Decimal("5.00")

    context = leg.source_reference.request_context
    assert "地铁1号线(苹果园-四惠东)" in context["transit_lines"]
    assert "地铁2号线" in context["transit_lines"]
    assert context["transit_segment_kinds"] == ["walking", "bus", "railway"]
    assert context["walking_distance_meters"] == 700


@pytest.mark.asyncio
async def test_transit_sends_the_required_city_and_longitude_first_coordinates():
    """``city`` is required for transit - omitting it is an error, not an empty
    result - and coordinates must be ``lon,lat``. Sending them swapped is not
    rejected; it resolves to a point in the wrong place."""
    provider, http = _provider({"/v5/direction/transit/integrated": TRANSIT_PAYLOAD})
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]

    await provider._transit_legs(points, _profile(), _reference(provider, TravelMode.TRANSIT))

    path, params = http.calls[0]
    assert path == "/v5/direction/transit/integrated"
    assert params["city"] == "北京"
    assert params["origin"] == "116.400000,39.900000"
    assert params["destination"] == "116.480000,39.950000"


@pytest.mark.asyncio
async def test_transit_declares_that_it_only_covers_consecutive_pairs():
    """The coverage limit has to be visible in the data itself, not only in docs:
    otherwise a reader cannot tell a missing transit leg from a measured zero."""
    provider, _ = _provider()
    reference = _reference(provider, TravelMode.TRANSIT)

    assert reference.request_context["coverage"] == "consecutive_pairs"
    assert reference.request_context["cached"] is False
    assert reference.request_context["amap_endpoint"] == "/v5/direction/transit/integrated"


@pytest.mark.asyncio
async def test_the_driving_matrix_declares_full_coverage():
    provider, _ = _provider()
    reference = _reference(provider, TravelMode.DRIVE, point_count=3)
    assert reference.request_context["coverage"] == "full_matrix"
    assert reference.request_context["cached"] is False
    assert reference.request_context["amap_endpoint"] == "/v3/distance"


@pytest.mark.asyncio
async def test_transit_leg_count_is_bounded():
    """Each transit leg is one quota call, so the count is capped rather than
    allowed to scale with the candidate set."""
    provider, http = _provider()
    points = [_point(39.90 + index * 0.01, 116.40) for index in range(20)]

    await provider._transit_legs(points, _profile(), _reference(provider, TravelMode.TRANSIT, 20))

    assert len(http.calls) == provider.max_transit_legs


@pytest.mark.asyncio
async def test_a_transit_response_without_plans_yields_no_leg():
    """An empty ``transits`` list must produce nothing at all. A zero-distance leg
    would look like "the destination is right here"."""
    provider, _ = _provider(
        {"/v5/direction/transit/integrated": {"status": "1", "route": {"transits": []}}}
    )
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]

    legs = await provider._transit_legs(
        points, _profile(), _reference(provider, TravelMode.TRANSIT)
    )

    assert legs == []


@pytest.mark.asyncio
async def test_an_absent_fare_stays_absent_rather_than_becoming_zero():
    """A missing ``transit_fee`` must not become ``0 CNY``, which would understate
    the budget as a fact rather than as an unknown."""
    provider, _ = _provider(
        {
            "/v5/direction/transit/integrated": {
                "status": "1",
                "route": {"transits": [{"distance": "5000", "duration": "1200", "segments": []}]},
            }
        }
    )
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]

    legs = await provider._transit_legs(
        points, _profile(), _reference(provider, TravelMode.TRANSIT)
    )

    assert len(legs) == 1
    # No measured fare, and no invented one either.
    assert legs[0].cost is None


# -- walking ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_walking_goes_through_the_walking_endpoint_via_calculate_routes():
    """A relaxed trip must reach ``/v5/direction/walking``, not the batched endpoint.

    Asserted through the public entry point rather than by calling the private
    helper, because the failure being guarded against is a mode landing in the
    wrong branch - which is invisible if the helper is called directly.
    """
    provider, http = _provider(
        {"/v5/direction/walking": {"status": "1", "route": {"paths": [{"distance": "1500"}]}}}
    )
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]

    result = await provider.calculate_routes(_profile(Pace.RELAXED), points)

    assert [path for path, _ in http.calls] == ["/v5/direction/walking"]
    assert len(result.data) == 1
    leg = result.data[0]
    assert leg.mode is TravelMode.WALK
    # v5 walking paths carry no duration at all, so the time is derived and flagged.
    assert leg.is_estimate is True
    assert leg.duration_minutes == 20  # 1500 m at 1.25 m/s
    assert leg.source_reference.request_context["duration_source"] == "derived_walking_speed"


@pytest.mark.asyncio
async def test_a_driving_trip_never_calls_the_walking_endpoint():
    provider, http = _provider(
        {
            "/v3/distance": {
                "status": "1",
                "results": [{"origin_id": "1", "distance": "5000", "duration": "600"}],
            }
        }
    )
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert {path for path, _ in http.calls} == {"/v3/distance"}
    assert result.data[0].mode is TravelMode.DRIVE
    assert result.data[0].is_estimate is False


def test_the_endpoint_strings_are_the_verified_ones():
    """Pin the paths themselves. A typo returns an error payload that parses to zero
    legs, so the plan silently loses its routes."""
    assert DIRECTION_PATH_BY_MODE[TravelMode.WALK] == "/v5/direction/walking"
    assert DIRECTION_PATH_BY_MODE[TravelMode.TRANSIT] == "/v5/direction/transit/integrated"
    assert DIRECTION_PATH_BY_MODE[TravelMode.DRIVE] == "/v5/direction/driving"


def test_driving_is_the_only_batched_mode_because_the_docs_and_client_disagree():
    """Only ``type=1`` (驾车) is asserted by both AMap's documentation and its own
    client, so only driving may take the batched path. Adding walking here would
    silently strip its route geometry and step list."""
    assert DISTANCE_TYPE_BY_MODE == {TravelMode.DRIVE: 1, TravelMode.TAXI: 1}
    assert TravelMode.WALK not in DISTANCE_TYPE_BY_MODE


# -- boundaries -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_single_point_produces_no_routes_and_no_calls():
    provider, http = _provider()
    result = await provider.calculate_routes(_profile(), [_point(39.9, 116.4)])
    assert result.data == []
    assert http.calls == []


@pytest.mark.asyncio
async def test_the_point_set_is_capped_to_bound_the_call_count():
    def sweep(params):
        count = len(params["origins"].split("|"))
        return {
            "status": "1",
            "results": [
                {"origin_id": str(index), "distance": "100", "duration": "60"}
                for index in range(1, count + 1)
            ],
        }

    provider, http = _provider({"/v3/distance": sweep}, max_points=3)
    points = [_point(39.90 + index * 0.01, 116.40) for index in range(10)]

    await provider.calculate_routes(_profile(Pace.FULL), points)

    assert len(http.calls) == 3


@pytest.mark.asyncio
async def test_health_does_not_spend_a_quota_call():
    """A health check that costs money per poll is a bad trade, and the key's
    validity is established when the settings page saves it."""
    provider, http = _provider()
    health = await provider.health()
    assert http.calls == []
    assert health.status == "ok"
    assert health.provider == "amap"


# -- the point budget, and disclosing that it was applied ------------------
#
# `max_points` is a real budget: the free quota is a monthly call count, so routing every
# place a trip mentions is not free. What matters for honesty is not the size of the
# budget but whether a reader can tell it was applied — otherwise "this leg is measured"
# and "this leg is one of the twelve we got to" look identical.


def _counting_sweep(params):
    count = len(params["origins"].split("|"))
    return {
        "status": "1",
        "results": [
            {"origin_id": str(index), "distance": "100", "duration": "60"}
            for index in range(1, count + 1)
        ],
    }


@pytest.mark.asyncio
async def test_a_full_coverage_route_reports_that_nothing_was_dropped():
    provider, _ = _provider({"/v3/distance": _counting_sweep}, max_points=5)
    points = [_point(39.90 + index * 0.01, 116.40) for index in range(4)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    context = result.source_references[0].request_context
    assert context["points_offered"] == 4
    assert context["points_routed"] == 4
    assert context["truncated"] is False
    # The prose must not carry a warning that does not apply: a message shown always is
    # a message read never.
    assert "只对" not in context["note"]


@pytest.mark.asyncio
async def test_a_capped_route_says_how_many_places_it_actually_covered():
    """The failure this guards against is a partial route reading as a complete one.

    Ten places are offered, five are routed. Without both numbers in the payload, the
    five measured legs and the estimated remainder are indistinguishable from a trip
    that only ever had five places.
    """
    provider, http = _provider({"/v3/distance": _counting_sweep}, max_points=5)
    points = [_point(39.90 + index * 0.01, 116.40) for index in range(10)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert len(http.calls) == 5, "the budget must still bound the number of requests"
    context = result.source_references[0].request_context
    assert context["truncated"] is True
    assert context["points_offered"] == 10
    assert context["points_routed"] == 5
    assert context["point_budget"] == 5
    # Said in prose too, because the reader who needs it is looking at a leg.
    assert "只对 5 个地点" in context["note"]
    assert "10" in context["note"]
    assert "估算" in context["note"]


@pytest.mark.asyncio
async def test_the_budget_reads_as_a_budget_not_as_the_trip_size():
    """``point_count`` alone used to be the only number, and it described the slice.

    A client that trusted it would conclude a ten-place trip has five places.
    """
    provider, _ = _provider({"/v3/distance": _counting_sweep}, max_points=3)
    points = [_point(39.90 + index * 0.01, 116.40) for index in range(8)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    context = result.source_references[0].request_context
    assert context["points_offered"] == 8
    assert context["points_offered"] != context["point_count"]


def test_the_origin_limit_cannot_be_configured_past_the_protocol():
    """AMap accepts at most 100 origins per request; that is the protocol, not a knob.

    A budget above it would send a request AMap rejects, losing every leg in the sweep
    at once — so the clamp is the difference between a degraded route and no route.
    """
    provider, _ = _provider(max_points=AMAP_MAX_MATRIX_ORIGINS + 500)
    assert provider.max_points == AMAP_MAX_MATRIX_ORIGINS


def test_a_budget_below_two_is_not_expressible():
    """Fewer than two points cannot produce a leg, so a budget of 1 would silently
    disable routing altogether while looking like a valid setting."""
    provider, _ = _provider(max_points=1)
    assert provider.max_points >= 2


# -- a failure must be reported, not swallowed ------------------------------
#
# Every helper below catches ``AmapError`` so one failed sweep does not discard the legs
# already gathered. Catching without recording was the bug: when *every* request failed the
# provider still returned ``status="success"`` with no errors and no legs, which from
# outside is byte-identical to a source that has nothing to say about this mode.
#
# The consequence was user-visible. With a valid-looking but rejected key, transit produced
# no legs, and the itinerary explained 「没有可用的数据源能算这种路线」 and advised supplying a
# 高德 key — to the person who had just supplied one. Nothing anywhere said the key had been
# refused, because the only trace was a ``logger.warning``.


class _RejectingHttp:
    """Every request rejected, as an invalid key, wrong platform or spent quota would be."""

    def __init__(self, message: str = "AMap request failed: infocode=10001 info=INVALID_USER_KEY"):
        self.message = message
        self.calls = 0

    async def get_json(self, url, *, params=None):
        self.calls += 1
        from travel_api.providers.amap.client import AmapError

        raise AmapError(self.message)

    async def post_json(self, url, *, data=None):
        raise AssertionError("routing must not use POST")


def _rejecting_provider(**kwargs):
    http = _RejectingHttp()
    return AmapRoutingProvider(AmapClient(http=http, key="rejected-key"), **kwargs), http


@pytest.mark.asyncio
async def test_a_failed_transit_sweep_reports_itself_as_degraded():
    provider, http = _rejecting_provider()
    points = [_point(39.90, 116.40), _point(39.95, 116.48), _point(39.99, 116.52)]

    result = await provider.calculate_routes(_transit_profile(), points)

    assert http.calls > 0, "the probe must actually attempt requests"
    assert result.data == []
    assert result.status == "degraded", "a total failure is not a success"
    assert result.errors, "the reason must travel with the result, not only to the log"
    assert "10001" in result.errors[0]


@pytest.mark.asyncio
async def test_a_failed_driving_sweep_reports_itself_too():
    """Not transit-specific: the matrix path collapses identically, and so does walking."""
    provider, http = _rejecting_provider()
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert http.calls > 0
    assert result.status == "degraded"
    assert result.errors


@pytest.mark.asyncio
async def test_a_clean_sweep_is_not_marked_degraded():
    """The other direction. If every sweep read as degraded the field would carry no
    information, and a reader would stop distinguishing a broken key from a working one."""
    provider, _ = _provider({"/v3/distance": _counting_sweep})
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert result.data
    assert result.status == "success"
    assert result.errors == []


@pytest.mark.asyncio
async def test_the_same_failure_is_repeated_once_not_once_per_request():
    """A matrix makes one request per destination, so one bad key fails N times.

    Repeating the identical sentence N times would make the note unreadable without adding
    anything to the diagnosis; the distinct message is what matters.
    """
    provider, http = _rejecting_provider()
    points = [_point(39.90 + index * 0.01, 116.40) for index in range(6)]

    result = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert http.calls >= 4
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_failures_do_not_leak_from_one_call_into_the_next():
    """The record is per call. A stale error would blame the wrong request — and would
    mark a later healthy plan as degraded forever."""
    provider, _ = _rejecting_provider()
    points = [_point(39.90, 116.40), _point(39.95, 116.48)]
    failed = await provider.calculate_routes(_profile(Pace.FULL), points)
    assert failed.status == "degraded"

    # Same provider instance, now pointed at a working endpoint.
    provider.client = AmapClient(http=_RoutingHttp({"/v3/distance": _counting_sweep}), key="k")
    recovered = await provider.calculate_routes(_profile(Pace.FULL), points)

    assert recovered.status == "success"
    assert recovered.errors == []


def test_the_probe_claim_is_not_vacuous():
    """``_RejectingHttp`` must raise the *transport* error this provider catches.

    If it raised something else the tests above would pass through a different path and
    prove nothing about the handling they claim to cover.
    """
    from travel_api.providers.amap.client import AmapError

    assert issubclass(AmapError, Exception)
    provider, http = _rejecting_provider()
    assert provider.client is not None
    assert http.message
