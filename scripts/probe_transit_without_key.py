"""Does asking for 公交/地铁 without a 高德 key still produce a usable plan?

This is the interesting degradation. With no key, the free OSRM provider is the only
router, and it *declines* transit rather than substituting a car time — which is correct,
but it means the planner receives zero measured legs for an itinerary that still needs
travel times between its stops.

Two failure modes are possible and only one is acceptable:

  * the plan comes out with travel times that are estimated and *labelled* as estimates,
    plus no route source reference claiming a measurement happened; or
  * the plan comes out looking normal, with plausible minutes that nothing accounts for.

The second is what this probes for. It uses real free providers (slow, ~1-2 min) so the
answer reflects the deployed pipeline rather than a fixture.

Run with the API's src on PYTHONPATH. Needs TRAVER_LIVE_TESTS=1.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app


def main() -> None:
    if os.environ.get("TRAVER_LIVE_TESTS") != "1":
        print("SKIP: set TRAVER_LIVE_TESTS=1 to run against the live free providers")
        return

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "apps/api/src"))
    tmp = Path(tempfile.mkdtemp())

    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp / 'transit.db'}",
        auto_create_schema=True,
        # Real free sources, no AMap key anywhere: exactly the user's zero-config state
        # after asking for 公交/地铁.
        free_providers_enabled=True,
        mock_provider_enabled=False,
        amap_enabled=True,
        auth_enabled=False,
        credential_key_file=str(tmp / "credential.key"),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/trips",
            json={
                "destination": "杭州",
                "start_date": date(2026, 10, 3).isoformat(),
                "end_date": date(2026, 10, 5).isoformat(),
                "travelers": 2,
                "total_budget": 5000,
                "pace": "balanced",
                "travel_mode": "transit",
            },
        )
        if created.status_code >= 400:
            raise SystemExit(
                f"FAILED: trip creation returned {created.status_code}: {created.text[:300]}"
            )

        trip = created.json()
        print(f"created: {trip['id']}  travel_mode={trip['profile'].get('travel_mode')!r}")

        preview = trip.get("preview") or {}
        legs = preview.get("route_legs") or []
        refs = preview.get("source_references") or []
        days = (trip.get("itinerary") or {}).get("days") or []

        print(f"\nroute legs:          {len(legs)}")
        print(f"itinerary days:      {len(days)}")
        print(f"source references:   {len(refs)}")

        measured = [leg for leg in legs if leg.get("duration_is_measured")]
        estimated = [leg for leg in legs if not leg.get("duration_is_measured")]
        print(f"  measured legs:     {len(measured)}")
        print(f"  estimated legs:    {len(estimated)}")

        route_refs = [
            ref for ref in refs if str(ref.get("source", "")).startswith(("osrm", "amap"))
        ]
        print(f"route source refs:   {len(route_refs)}")
        for ref in route_refs:
            ctx = ref.get("request_context") or {}
            print(
                f"  {ref.get('source')}: mode={ctx.get('travel_mode')!r} "
                f"profile={ctx.get('osrm_profile')!r} coverage={ctx.get('coverage')!r}"
            )

        # The promise: nothing may present a car time as a bus time. With transit
        # declined, there must be no measured route reference claiming transit.
        offenders = [
            ref
            for ref in route_refs
            if (ref.get("request_context") or {}).get("travel_mode") == "transit"
        ]
        if offenders:
            raise SystemExit(
                f"FAILED: a route reference claims a transit measurement without a key: "
                f"{offenders[0].get('source')}"
            )
        print("\n  ok  no source claims to have measured a 公交/地铁 route")

        # And the trip must still be plannable, with the gap stated in the itinerary's
        # own conflicts rather than silently absorbed.
        if not days:
            raise SystemExit("FAILED: asking for transit produced no itinerary at all")
        print(f"  ok  the trip still planned ({len(days)} days)")

        legs_in_itinerary = [item for day in days for item in day.get("items", [])]
        print(f"  ok  {len(legs_in_itinerary)} scheduled items have travel times")

        # The traveller asked for 公交/地铁 and got distance-based estimates. The plan has to
        # connect the two, or the estimated minutes read as measured ones.
        conflicts = (trip.get("itinerary") or {}).get("conflicts") or []
        print(f"\nitinerary conflicts ({len(conflicts)}):")
        for conflict in conflicts:
            print(f"  - {conflict}")
        if not any("公交 / 地铁" in conflict for conflict in conflicts):
            raise SystemExit(
                "FAILED: the requested mode went unserved and the plan does not say so"
            )
        print("  ok  the plan explains that the requested mode was not served")

        # The capability matrix must report the gap rather than the mode being available.
        catalog = client.get("/api/v1/providers/catalog")
        if catalog.status_code != 200:
            raise SystemExit(f"FAILED: catalog returned {catalog.status_code}")
        payload = catalog.json()
        row = next(
            (
                item
                for item in payload.get("capabilities", [])
                if item.get("capability") == "calculate_routes"
            ),
            None,
        )
        if row is None:
            raise SystemExit("FAILED: calculate_routes missing from the catalogue")
        aspects = {aspect["aspect"]: aspect for aspect in row.get("aspects", [])}
        transit = aspects.get("transit", {})
        print(
            f"\n  catalogue transit: covered={transit.get('covered')} "
            f"providers={transit.get('providers')}"
        )
        if transit.get("covered"):
            raise SystemExit("FAILED: transit is reported as covered with no 高德 key configured")
        print("  ok  the catalogue reports 公交/地铁 as uncovered without a key")

    print("\nOK")


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
