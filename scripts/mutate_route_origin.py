"""Does the Null-Island guard actually guard? Restore the defect and see.

The whole risk with a regression test written *after* the fix is that it passes for a reason
unrelated to the fix. So this harness restores the original two lines — a real hotel id beside a
`(0.0, 0.0)` placeholder — and requires the new tests to fail. If they pass, the tests are
describing something other than the defect and are worth nothing.
"""

from __future__ import annotations

from _mutation_harness import ROOT, Case, run_harness

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

#: Built from `MUTATIONS` rather than rewritten as `Case(...)` literals, so the anchored `old`
#: strings above stay exactly as they were written. Every one of them is load-bearing: two of the
#: four call sites are textually near-identical and the anchoring is what keeps a replacement from
#: landing in the wrong method.
CASES = [Case(name, SERVICE, old, new) for name, old, new in MUTATIONS]

if __name__ == "__main__":
    raise SystemExit(run_harness(files=(SERVICE,), cases=CASES, test_targets=TESTS))
