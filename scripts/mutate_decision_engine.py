"""Does the decision-engine fix actually have guards?

Three separate below-the-bar findings, all reproduced, none of them caught by any existing test.
Written after the fix, so each one is restored here and the new tests must fail.

The first is the interesting one. `Hotel.rating` is `None` for every hotel in every real
deployment — OSM's `stars` is a self-declared classification and 高德 lists `rating` among its own
`absent_fields` — but `providers/mock.py` returns 4.8 and 4.6, and every test in
`test_hotel_scoring.py` uses that fixture. So a scorer that treated an unpublished rating as zero
passed the whole suite. The guard works only because the new tests build hotels with
`rating=None` explicitly, which is the one thing the fixture could not do for them.
"""

from __future__ import annotations

from _mutation_harness import ROOT, Case, run_harness

ENGINE = ROOT / "apps/api/src/travel_api/services/decision_engine.py"
ENGINE_BEFORE = """        if hotel.rating is not None:
            measured["rating"] = hotel.rating / 5"""

CASES = [
    Case(
        "M1 the original defect: an unpublished rating scores as the worst possible one",
        ENGINE,
        ENGINE_BEFORE,
        """        measured["rating"] = (hotel.rating or 0) / 5""",
    ),
    Case(
        "M2 the rating is always dropped, even when a source published one",
        ENGINE,
        ENGINE_BEFORE,
        """        if False and hotel.rating is not None:
            measured["rating"] = hotel.rating / 5""",
    ),
    Case(
        "M3 the rating is measured but not normalised to 0..1",
        ENGINE,
        ENGINE_BEFORE,
        """        if hotel.rating is not None:
            measured["rating"] = hotel.rating""",
    ),
    Case(
        "M4 the destination slot is filled from the hotel's own city again",
        ENGINE,
        'lines.append(f"酒店没有上报所在城市，无法与目的地（{destination}）比较")',
        "lines.append(f\"酒店没有上报所在城市，无法与目的地（{hotel.location.city or '未知'}）比较\")",
    ),
]

if __name__ == "__main__":
    raise SystemExit(
        run_harness(
            files=(ENGINE,),
            cases=CASES,
            test_targets="tests/unit/test_hotel_scoring.py tests/unit/test_user_facing_prose.py",
        )
    )
