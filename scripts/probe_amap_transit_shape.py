"""Verify the real 高德 v5 transit response shape against a real key.

The transit parser reads ``distance`` and ``duration`` tolerantly, because whether v5 puts the
duration at ``transits[].duration`` or only inside ``transits[].cost`` could not be confirmed
from AMap's documentation in the environment this was written in. The unit-test fixture
asserts the top-level shape — and a fixture written from the same assumption as the parser
cannot detect a wrong assumption. Both would be consistently wrong together.

That is the failure worth taking seriously here: if the field paths are wrong, no leg is
produced for any pair, and 公交/地铁 — the entire reason to supply a 高德 key — silently yields
nothing. Everything else in the chain still looks healthy, and the code reports "no available
source" unless the request itself failed.

So this runs one real transit request and reports, factually, what came back: which fields
exist, which one the parser used, and whether a leg was produced. It asserts only what must
hold regardless of the shape — that the request succeeded and that the keys the parser needs
are present *somewhere*, in one of the accepted locations. If AMap has changed its shape, this
says so by name instead of leaving it to be inferred from an empty itinerary.

Needs a key:

    $env:TRAVER_AMAP_KEY = '...'      # 高德 Web 服务 Key
    python scripts/probe_amap_transit_shape.py

Optional: TRAVER_AMAP_ORIGIN / TRAVER_AMAP_DESTINATION as ``lon,lat``, and
TRAVER_AMAP_CITY for the (required) transit city parameter.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api/src"))

from travel_api.domain.models import (  # noqa: E402
    Pace,
    RoutePoint,
    TravelMode,
    TripProfile,
)
from travel_api.providers.amap.client import AmapClient, require_success  # noqa: E402
from travel_api.providers.amap.routing import AmapRoutingProvider  # noqa: E402
from travel_api.providers.free.http import FreeHttpClient  # noqa: E402

#: 天安门 → 国贸, a pair that certainly has bus and subway service.
DEFAULT_ORIGIN = "116.397428,39.90923"
DEFAULT_DESTINATION = "116.461111,39.908722"
DEFAULT_CITY = "北京"


def _keys(payload: object, prefix: str = "", depth: int = 0) -> list[str]:
    """Every key path in a JSON object, so the shape can be read at a glance."""
    if depth > 3 or not isinstance(payload, dict):
        return []
    found: list[str] = []
    for key, value in payload.items():
        path = f"{prefix}{key}"
        found.append(path)
        if isinstance(value, dict):
            found.extend(_keys(value, f"{path}.", depth + 1))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            found.extend(_keys(value[0], f"{path}[].", depth + 1))
    return found


async def main() -> int:
    key = os.environ.get("TRAVER_AMAP_KEY", "").strip()
    if not key:
        print("SKIP: set TRAVER_AMAP_KEY to run this against the real service")
        return 0

    origin_raw = os.environ.get("TRAVER_AMAP_ORIGIN", DEFAULT_ORIGIN)
    destination_raw = os.environ.get("TRAVER_AMAP_DESTINATION", DEFAULT_DESTINATION)
    city = os.environ.get("TRAVER_AMAP_CITY", DEFAULT_CITY)

    def point(raw: str) -> RoutePoint:
        longitude, latitude = (float(part) for part in raw.split(","))
        return RoutePoint(id=uuid4(), latitude=latitude, longitude=longitude)

    # The raw payload is fetched alongside the parsed leg so the shape can be printed even
    # when parsing produced nothing — which is the case this probe exists to diagnose.
    captured: dict = {}

    class _Capturing:
        def __init__(self, inner) -> None:
            self.inner = inner

        async def get_json(self, url, *, params=None):
            payload = await self.inner.get_json(url, params=params)
            if "transit/integrated" in url:
                captured["payload"] = payload
                captured["params"] = dict(params or {})
            return payload

        async def post_json(self, url, *, data=None):
            return await self.inner.post_json(url, data=data)

    client = AmapClient(
        http=_Capturing(
            FreeHttpClient(
                user_agent="traver-shape-probe/1.0",
                timeout_seconds=30.0,
                host_intervals={"restapi.amap.com": 1.0},
            )
        ),
        key=key,
    )
    provider = AmapRoutingProvider(client)
    profile = TripProfile(
        destination=city,
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        pace=Pace.BALANCED,
        travel_mode=TravelMode.TRANSIT,
    )

    legs = await provider.calculate_routes(profile, [point(origin_raw), point(destination_raw)])

    payload = captured.get("payload")
    problems: list[str] = []

    if payload is None:
        problems.append("no transit response was captured at all")
    else:
        try:
            require_success(payload, operation="transit probe")
            print("  request            : success")
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            print(f"  request            : FAILED {exc}")
            return 1

        paths = _keys(payload)
        transit_paths = [path for path in paths if path.startswith("route.transits[]")]
        print(f"  top-level keys     : {sorted(payload.keys())}")
        print(f"  route keys         : {sorted((payload.get('route') or {}).keys())}")
        print(
            f"  transit[0] keys    : {sorted(k.split('[]', 1)[1].lstrip('.').split('.')[0] for k in transit_paths)}"
        )

        transits = ((payload.get("route") or {}).get("transits")) or []
        if not transits:
            problems.append(
                "route.transits is empty: this pair has no transit plan, so the shape "
                "cannot be confirmed here — try a different origin/destination pair"
            )
        else:
            first = transits[0]
            cost = first.get("cost") if isinstance(first.get("cost"), dict) else {}
            print(f"  transits[].distance: {first.get('distance')!r}")
            print(f"  transits[].duration: {first.get('duration')!r}")
            print(f"  cost.duration      : {cost.get('duration')!r}")
            print(f"  transit_fee        : {cost.get('transit_fee')!r}")
            if first.get("distance") in (None, ""):
                problems.append("transits[].distance is absent — the parser has no fallback")
            if first.get("duration") in (None, "") and cost.get("duration") in (None, ""):
                problems.append(
                    "no duration in transits[].duration or transits[].cost.duration — "
                    "the transit leg cannot be built"
                )

    print(f"\n  legs produced      : {len(legs)}")
    if legs:
        leg = legs[0]
        context = leg.source_reference.request_context
        print(f"  distance_meters    : {leg.distance_meters}")
        print(f"  duration_minutes   : {leg.duration_minutes}")
        print(f"  is_estimate        : {leg.is_estimate}")
        print(f"  distance_field     : {context.get('distance_field')}")
        print(f"  duration_field     : {context.get('duration_field')}")
        print(f"  transit_lines      : {context.get('transit_lines')}")
        print(f"  segment kinds      : {context.get('transit_segment_kinds')}")
        if leg.is_estimate:
            problems.append("a transit leg must never be marked as an estimate")
    else:
        problems.append(
            "no leg was produced from a successful response — the field paths in "
            "AmapRoutingProvider._parse_transit do not match this service's real shape"
        )

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: the real v5 transit shape is one the parser reads")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
