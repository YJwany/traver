"""Does the freshness panel receive what it needs from the real API?

The panel has its own tests, but they run against a hand-built preview. What they cannot
check is whether the server actually sends `age_unknown_facts` on a real plan — which is the
whole reason the panel exists, and exactly the gap that produced this defect in the first
place: a correct render of a field nobody sent.

So this drives a real plan through the HTTP API and inspects the payload the browser would
get. It asserts the field is present rather than absent (an absent field must not be readable
as "nothing to report"), that the free stack really does produce unverifiable facts, and that
each finding carries enough to be checked by hand.

Needs TRAVER_LIVE_TESTS=1 and the free providers. Takes ~70 s.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api/src"))

from fastapi.testclient import TestClient  # noqa: E402
from travel_api.config import Settings  # noqa: E402
from travel_api.main import create_app  # noqa: E402


def main() -> int:
    if os.environ.get("TRAVER_LIVE_TESTS") != "1":
        print("SKIP: set TRAVER_LIVE_TESTS=1 to run against the live free providers")
        return 0

    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp / 'fresh.db'}",
        auto_create_schema=True,
        free_providers_enabled=True,
        mock_provider_enabled=False,
        auth_enabled=False,
        credential_key_file=str(tmp / "credential.key"),
    )

    problems: list[str] = []
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/trips",
            json={
                "destination": "杭州",
                "start_date": date(2026, 10, 3).isoformat(),
                "end_date": date(2026, 10, 5).isoformat(),
                "travelers": 2,
                "total_budget": 5000,
            },
        )
        if created.status_code >= 400:
            raise SystemExit(f"trip creation failed: {created.status_code} {created.text[:200]}")

        trip = created.json()
        # Read it back over HTTP too: the panel renders a *reloaded* trip, and a field that
        # survives creation but not reload would show up as missing in the browser.
        reloaded = client.get(f"/api/v1/trips/{trip['id']}").json()

    for label, payload in (("created", trip), ("reloaded", reloaded)):
        preview = payload.get("preview") or {}
        if "age_unknown_facts" not in preview:
            problems.append(f"{label}: the payload has no age_unknown_facts key at all")
            continue
        if "stale_facts" not in preview:
            problems.append(f"{label}: the payload has no stale_facts key")

        stale = preview.get("stale_facts") or []
        unknown = preview.get("age_unknown_facts") or []
        refs = preview.get("source_references") or []
        print(
            f"{label:9} references={len(refs):3}  stale={len(stale):3}  "
            f"unverifiable={len(unknown):3}"
        )

        # The reason this panel exists: the free stack publishes an observation time for POI
        # data but not for weather, Wikipedia or the holiday calendar, so the stale list is
        # usually empty while a large share of references cannot be checked at all.
        if len(refs) > 1 and not unknown:
            problems.append(
                f"{label}: {len(refs)} references but nothing unverifiable — "
                f"the free stack does not publish an age for most of them, so this is wrong"
            )

        for finding in unknown:
            if finding.get("reason") != "age_unknown":
                problems.append(f"{label}: unexpected reason {finding.get('reason')!r}")
            for key in ("source", "data_type", "fetched_at", "evaluated_at", "observed_at_basis"):
                if not finding.get(key):
                    problems.append(f"{label}: finding for {finding.get('source')!r} lacks {key}")
            # An age-unknown finding must not report an age it does not have.
            if "data_age_seconds" not in finding:
                problems.append(f"{label}: finding lacks data_age_seconds")

        # And the two lists must not overlap: a fact cannot be both provably old and of
        # unknowable age.
        stale_keys = {(f.get("source"), f.get("source_id")) for f in stale}
        unknown_keys = {(f.get("source"), f.get("source_id")) for f in unknown}
        if stale_keys & unknown_keys:
            problems.append(f"{label}: a fact appears in both lists: {stale_keys & unknown_keys}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: the freshness panel's field arrives, and is neither empty nor conflated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
