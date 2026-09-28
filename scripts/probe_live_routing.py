"""What the live routing actually returns, for the record and for the docs.

Run with the proxy up:

    $env:TRAVER_LIVE_TESTS='1'; python scripts/probe_live_routing.py

Reports one real city trip's route legs so the claims in the docs rest on observed numbers
rather than on the shape of the code.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

from travel_api.domain.models import Pace, TripProfile
from travel_api.providers.free import build_free_providers


async def main() -> None:
    providers, http = build_free_providers(user_agent="traver-live-probe/0.1", language="zh")
    routing = next(p for p in providers if getattr(p, "name", "") == "osrm")
    print(f"server      : {routing.server.label} ({routing.server.host})")
    print(f"profiles    : {sorted(routing.server.prefixes)}")
    print(f"freshness   : {routing.server.freshness_note}")

    # Geocode, then take the first few real POIs as route points.
    from travel_api.providers.free.overpass import OverpassPoiProvider

    poi = next(p for p in providers if isinstance(p, OverpassPoiProvider))
    from travel_api.domain.models import RoutePoint

    for pace, label in ((Pace.RELAXED, "步行（relaxed）"), (Pace.FULL, "驾车（full）")):
        profile = TripProfile(
            destination="杭州",
            start_date=date(2026, 10, 3),
            end_date=date(2026, 10, 5),
            travelers=2,
            pace=pace,
        )
        attractions = (await poi.search_attractions(profile)).data
        points = [
            RoutePoint(
                id=item.id,
                latitude=item.location.coordinates.latitude,
                longitude=item.location.coordinates.longitude,
            )
            for item in attractions[:5]
        ]
        if len(points) < 2:
            print(f"\n{label}: not enough attractions to route")
            continue
        result = await routing.calculate_routes(profile, points)
        print(f"\n=== {label} — {len(points)} points, {len(result.data)} legs ===")
        print(
            f"{'mode':7} {'distance':>9} {'duration':>9} {'km/h':>6}  {'is_estimate':>11}  source"
        )
        for leg in result.data[:8]:
            speed = leg.distance_meters / 1000 / (leg.duration_minutes / 60)
            ctx = leg.source_reference.request_context
            print(
                f"{leg.mode.value:7} {leg.distance_meters:>7} m {leg.duration_minutes:>7} min "
                f"{speed:>6.1f}  {str(leg.is_estimate):>11}  {ctx['duration_source']}"
            )
        measured = sum(
            1 for leg in result.data if leg.source_reference.request_context["duration_is_measured"]
        )
        print(f"measured durations: {measured}/{len(result.data)}")

    await http.aclose()


if __name__ == "__main__":
    if os.getenv("TRAVER_LIVE_TESTS") != "1":
        raise SystemExit("set TRAVER_LIVE_TESTS=1 to run this against the live services")
    asyncio.run(main())
