"""Does the Null-Island guard actually guard? Restore the defect and see.

The whole risk with a regression test written *after* the fix is that it passes for a reason
unrelated to the fix. So this harness restores the original two lines — a real hotel id beside a
`(0.0, 0.0)` placeholder — and requires the new tests to fail. If they pass, the tests are
describing something other than the defect and are worth nothing.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(r"C:\Users\27875\Desktop\Traver")
SERVICE = ROOT / "apps/api/src/travel_api/services/trip_service.py"
TESTS = "tests/integration/test_route_origin.py"

HELPER_CALL = "        hotel_id, hotel_origin = self._selected_hotel_origin(preview, trip.selected_hotel_offer_id)"

#: The shapes the fix removed: the helper's result replaced by the original hard-coded pair.
#: `_selected_hotel_id` is what the old code called, with the placeholder beside it.
#:
#: Each `old` is anchored on its own enclosing `def`, because two of the four call sites are
#: textually near-identical — `_recalculate_attractions` and `_remove_attraction` differ only in
#: which list they pass. An unanchored pattern matches both and the replacement then fails the
#: harness's own "must match exactly once" check, which is how this was caught rather than
#: silently mutating the wrong method.
MUTATIONS = [
    (
        "M1 _recalculate_attractions estimates from (0.0, 0.0) again",
        """    def _recalculate_attractions(self, trip: Trip, attractions) -> Trip:
        preview = trip.preview
        selected_offer = self._selected_offer(preview, trip.selected_hotel_offer_id)
"""
        + HELPER_CALL
        + """
        itinerary = RouteOptimizer().optimize(
            trip.profile,
            hotel_id,
            hotel_origin,
            attractions,""",
        """    def _recalculate_attractions(self, trip: Trip, attractions) -> Trip:
        preview = trip.preview
        selected_offer = self._selected_offer(preview, trip.selected_hotel_offer_id)
        hotel_id = self._selected_hotel_id(preview, trip.selected_hotel_offer_id)
        itinerary = RouteOptimizer().optimize(
            trip.profile,
            hotel_id,
            (0.0, 0.0),
            attractions,""",
    ),
    (
        "M2 _remove_attraction estimates from (0.0, 0.0) again",
        """        if len(attractions) == len(preview.attractions):
            raise TripRevisionError("attraction not found")
"""
        + HELPER_CALL
        + """
        itinerary = RouteOptimizer().optimize(
            trip.profile,
            hotel_id,
            hotel_origin,
            attractions,""",
        """        if len(attractions) == len(preview.attractions):
            raise TripRevisionError("attraction not found")
        itinerary = RouteOptimizer().optimize(
            trip.profile,
            self._selected_hotel_id(preview, trip.selected_hotel_offer_id),
            (0.0, 0.0),
            attractions,""",
    ),
]


def run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS.split(), "-q", "--no-header", "-p", "no:logging"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "apps/api/src"), "PYTHONUTF8": "1"},
    )
    return proc.returncode, proc.stdout[-1200:]


def main() -> int:
    original = SERVICE.read_text(encoding="utf-8")

    def restore() -> None:
        SERVICE.write_text(original, encoding="utf-8")

    restore()
    code, out = run()
    if code != 0:
        print(f"BASELINE FAILS — the tests do not pass on the fixed tree:\n{out}")
        return 1
    print("baseline (fixed tree): all tests pass\n")

    uncaught: list[str] = []
    for name, old, new in MUTATIONS:
        restore()
        text = SERVICE.read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"NOT APPLIED ({text.count(old)} matches): {name}")
            uncaught.append(name)
            continue
        SERVICE.write_text(text.replace(old, new), encoding="utf-8")
        code, out = run()
        print(f"{'caught' if code != 0 else 'NOT CAUGHT':11} {name}")
        if code == 0:
            uncaught.append(name)

    restore()
    print()
    if uncaught:
        print(f"{len(uncaught)} mutation(s) not accounted for:")
        for name in uncaught:
            print(f"  - {name}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
