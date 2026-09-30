"""Which quantity in the OSM fixture is invariant, and which decays?

Written to settle an argument with a test that had gone red. The test asserted
``data_age_seconds < 3 * 86400`` against a fixture whose snapshot is a fixed instant, and its
own comment claimed it was "asserted as a ratio rather than an absolute". Both the comment and
the assertion were wrong, in different ways, and the arithmetic says which form is right:

    data_age_seconds  = now - snapshot_instant
    median_seconds    = now - median_edit_instant

    ratio    = (now - median_edit) / (now - snapshot)   -> 1 as now -> infinity   DECAYS
    difference = snapshot_instant - median_edit_instant                          INVARIANT

So the difference is the shape of the claim "the reported age is about our copy, not about the
facts in it", and it is what the test now asserts. The output also checks the difference against
``observed_at - oldest_at``, two absolute fixture instants, to show the invariant is real rather
than a number copied from a run.

Run::

    python scripts/probe_element_age_timebomb.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests/unit"))

import test_free_providers as fixture  # noqa: E402


async def main() -> None:
    http, _, poi, *_ = fixture.build_stack()
    try:
        hotels = (await poi.search_hotels(fixture.make_profile())).data
        reference = hotels[0].source_references[0]
        age = reference.element_age

        print("instants the fixture pins:")
        print("  observed_at (the snapshot OSM published) :", reference.observed_at.isoformat())
        print("  oldest_at   (an element's last edit)     :", age.oldest_at.isoformat())
        print("  fetched_at  (ours)                       :", reference.fetched_at.isoformat())
        print()
        print("quantities measured back from `now`:")
        print("  data_age_seconds                         :", reference.data_age_seconds)
        print("  median_seconds                           :", age.median_seconds)
        print("  oldest_seconds                           :", age.oldest_seconds)
        print()

        median_gap = age.median_seconds - reference.data_age_seconds
        oldest_gap = age.oldest_seconds - reference.data_age_seconds
        ratio = age.median_seconds / max(reference.data_age_seconds, 1)

        print("DIFFERENCE median - snapshot :", median_gap, f"= {median_gap / 86400:.1f} days")
        print("DIFFERENCE oldest - snapshot :", oldest_gap, f"= {oldest_gap / 86400:.1f} days")
        print("RATIO      median / snapshot :", f"{ratio:.1f}", "  <- this is the one that decays")
        print()
        print("the oldest gap against two absolute instants, which is why it cannot decay:")
        print(
            "  observed_at - oldest_at    :",
            (reference.observed_at - age.oldest_at).total_seconds(),
        )
        print(
            "  equal:",
            abs(oldest_gap - (reference.observed_at - age.oldest_at).total_seconds()) < 2,
        )
    finally:
        await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
