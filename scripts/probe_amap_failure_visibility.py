"""Does a broken AMap key look different from a source that simply cannot serve the mode?

AMap's routing catches ``AmapError`` inside each per-pair helper and returns an empty list,
so the failure never reaches ``_safe_call`` in the aggregation layer — which means the
``ProviderResult`` comes back with the default ``status="success"`` and no errors. Two very
different situations then look identical downstream:

  * no source can serve 公交/地铁 (no key configured: AMap is not even built, OSRM declines
    transit) — nothing is wrong, and the honest advice is "supply a 高德 key";
  * a 高德 key *is* configured and every transit call failed (invalid key, expired quota,
    network) — something is wrong, and "supply a 高德 key" is advice the user already
    followed.

The user-facing disclosure currently says 「没有可用的数据源能算这种路线」 in both cases. For
the second that is false, and the remedy it prints points at a credential the user already
supplied.

This probe demonstrates the collapse without needing a real key or network: it builds a
routing provider whose client always fails and prints what the result claims about itself.
"""

from __future__ import annotations

import asyncio
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
from travel_api.providers.amap.client import AmapClient, AmapError  # noqa: E402
from travel_api.providers.amap.routing import AmapRoutingProvider  # noqa: E402


class _AlwaysFailing:
    """A 高德 endpoint that rejects everything, as an invalid key or spent quota would."""

    def __init__(self) -> None:
        self.attempts = 0

    async def get_json(self, url, *, params=None):
        self.attempts += 1
        raise AmapError("AMap request failed: INVALID_USER_KEY (10001)")

    async def post_json(self, url, *, data=None):
        raise AmapError("routing must not POST")


def _point() -> RoutePoint:
    return RoutePoint(id=uuid4(), latitude=39.9, longitude=116.4)


async def main() -> int:
    http = _AlwaysFailing()
    provider = AmapRoutingProvider(AmapClient(http=http, key="a-key-that-amap-rejects"))
    profile = TripProfile(
        destination="北京",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 3),
        pace=Pace.BALANCED,
        travel_mode=TravelMode.TRANSIT,
    )
    points = [_point() for _ in range(3)]

    result = await provider.calculate_routes(profile, points)

    print(f"  requests attempted : {http.attempts}")
    print(f"  legs returned      : {len(result.data)}")
    print(f"  status             : {result.status!r}")
    print(f"  errors             : {result.errors}")
    print(f"  references         : {len(result.source_references)}")

    # Also show that the same is true of the distance-matrix path, so this is not specific
    # to transit: a driving plan with a broken key reports success and zero legs too.
    driving = profile.model_copy(update={"travel_mode": TravelMode.DRIVE})
    http2 = _AlwaysFailing()
    provider2 = AmapRoutingProvider(AmapClient(http=http2, key="a-key-that-amap-rejects"))
    drive_result = await provider2.calculate_routes(driving, points)
    print(
        f"\n  [driving] attempts={http2.attempts} legs={len(drive_result.data)} "
        f"status={drive_result.status!r} errors={drive_result.errors}"
    )

    problems: list[str] = []
    if http.attempts == 0:
        problems.append("no request was attempted, so this probe proves nothing")
    if result.status == "success" and not result.errors:
        problems.append(
            "every request failed, yet the result reports success with no errors — "
            "an invalid key is indistinguishable from a source that cannot serve the mode"
        )
    if drive_result.status == "success" and not drive_result.errors:
        problems.append("the driving path collapses the same way, so this is not transit-specific")

    if problems:
        print(f"\nCONFIRMED ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: a failed request is reported as a failure")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
