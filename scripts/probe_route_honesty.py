"""Does the provider actually behave the way its declarations claim?

This is the check the original capacity bug lacked. The claim/behaviour gap is where the
false coverage lived: Overpass *declared* nothing and *returned* nothing, and the matrix
called it full coverage because nobody compared the two.

So this probe compares, for each matrix aspect the provider claims to serve, that it really
produces a leg for the corresponding travel mode - and, for each it claims not to, that it
declines instead of substituting.

The two OSRM deployments are the interesting pair. FOSSGIS honours a real foot profile;
the demo server answers a foot request with car durations and HTTP 200.
"""

from __future__ import annotations

import asyncio
from datetime import date
from uuid import uuid4

import httpx

from travel_api.domain.models import (
    Pace,
    RoutePoint,
    TravelMode,
    TripProfile,
)
from travel_api.providers.base import unserved_aspects_of
from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.free.osrm import (
    DEFAULT_OSRM_BASE_URL,
    FOSSGIS_ROUTING_HOST,
    OSRM_DEMO_HOST,
    OsmRoutingProvider,
)

#: A real walk-versus-drive pair, as the live servers report it.
FOOT_MATRIX = {"code": "Ok", "distances": [[0, 8386], [8420, 0]], "durations": [[0, 6708], [6740, 0]]}
CAR_MATRIX = {"code": "Ok", "distances": [[0, 9479], [9490, 0]], "durations": [[0, 706], [710, 0]]}


def _transport(*, foot_is_really_foot: bool):
    """A stand-in for either deployment.

    ``foot_is_really_foot`` is the whole difference between the two servers: FOSSGIS answers a
    foot request with pedestrian data, the demo answers it with the car one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "foot" in path:
            return httpx.Response(200, json=FOOT_MATRIX if foot_is_really_foot else CAR_MATRIX)
        if "bike" in path:
            return httpx.Response(200, json=CAR_MATRIX)
        return httpx.Response(200, json=CAR_MATRIX)

    return httpx.MockTransport(handler)


def _client(transport) -> FreeHttpClient:
    return FreeHttpClient(
        user_agent="TraverProbe/0.1",
        retry_count=0,
        transport=transport,
        host_intervals={FOSSGIS_ROUTING_HOST: 0.0, OSRM_DEMO_HOST: 0.0},
    )


def _profile(pace: Pace) -> TripProfile:
    return TripProfile(
        destination="鏉窞",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
        pace=pace,
    )


def _points() -> list[RoutePoint]:
    return [
        RoutePoint(id=uuid4(), latitude=30.2489, longitude=120.1550),
        RoutePoint(id=uuid4(), latitude=30.2410, longitude=120.1010),
    ]


async def probe() -> None:
    # Built once: the legs echo back the ids they were given, so regenerating them per call
    # would make every lookup miss.
    points = _points()
    first = points[0]
    for label, base_url, foot_real in (
        ("FOSSGIS", DEFAULT_OSRM_BASE_URL, True),
        ("demo", f"https://{OSRM_DEMO_HOST}", False),
    ):
        http = _client(_transport(foot_is_really_foot=foot_real))
        provider = OsmRoutingProvider(http=http, base_url=base_url)
        unserved = unserved_aspects_of(provider, "calculate_routes")
        print(f"\n=== {label} ({base_url}) ===")
        print(f"  honoured profiles : {sorted(provider.server.prefixes)}")
        print(f"  declared unserved : {sorted(unserved)}")

        for mode, pace in ((TravelMode.WALK, Pace.RELAXED), (TravelMode.DRIVE, Pace.FULL)):
            result = await provider.calculate_routes(_profile(pace), points)
            if not result.data:
                print(f"  {mode.value:8} -> DECLINED (no legs)")
                continue
            leg = next(leg for leg in result.data if leg.origin_id == first.id)
            ctx = leg.source_reference.request_context
            print(
                f"  {mode.value:8} -> {leg.distance_meters:>6} m  {leg.duration_minutes:>3} min  "
                f"is_estimate={leg.is_estimate!s:5}  profile={ctx['osrm_profile']:8} "
                f"source={ctx['duration_source']}"
            )

        # Transit is unrequestable through mode_for_pace today; pin that the provider would
        # decline rather than return a car time under a bus label.
        print(f"  transit  -> serves_mode={provider.serves_mode(TravelMode.TRANSIT)}")
        print(f"  walking  -> serves_mode={provider.serves_mode(TravelMode.WALK)}")
        await http.aclose()


if __name__ == "__main__":
    asyncio.run(probe())

