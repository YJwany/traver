"""Does the RollingGo guard actually guard? Put each defect back and see.

The provider's honesty rules are the kind that fail silently: a rate attached to the wrong
hotel, an estimate reported as provider-reported freshness, a room that needs requesting shown
as available. Every one of them produces a plausible-looking itinerary, so nothing downstream
complains. That makes the tests the only thing standing between the rule and its removal — and
a test that does not fail when the rule is deleted is worth nothing.

So each case restores a real defect and requires `tests/unit/test_rollinggo_provider.py` to
notice. If a case is not caught, either the guard is missing or this harness's target suite
does not reach the code it mutates.

    python scripts/mutate_rollinggo.py
"""

from __future__ import annotations

from _mutation_harness import ROOT, Case, run_harness

HOTELS = ROOT / "apps/api/src/travel_api/providers/rollinggo/hotels.py"
CLIENT = ROOT / "apps/api/src/travel_api/providers/rollinggo/client.py"
TESTS = "tests/unit/test_rollinggo_provider.py"

CASES = [
    Case(
        "M1 the supplier id is replaced by our own UUID in the request",
        HOTELS,
        """        for reference in hotel.source_references:
            if reference.source == ROLLINGGO_PROVIDER_ID:
                return reference.source_id
        return None""",
        """        for reference in hotel.source_references:
            if reference.source == ROLLINGGO_PROVIDER_ID:
                return str(hotel.id)
        return None""",
    ),
    Case(
        "M2 a hotel from another source is priced anyway",
        HOTELS,
        """            source_id = self._source_id_of(hotel)
            if source_id is None:
                continue""",
        """            source_id = self._source_id_of(hotel) or str(hotel.id)
            if source_id is None:
                continue""",
    ),
    Case(
        "M3 our fetch time is stamped as the data's own time",
        HOTELS,
        "            observed_at_basis=ObservationBasis.SOURCE_SILENT,",
        "            observed_at_basis=ObservationBasis.PROVIDER_REPORTED,",
    ),
    Case(
        "M4 a room on request is reported as available",
        HOTELS,
        """        availability = (
            None
            if on_request
            else _to_int(_first(row, ("availability", "availableCount", "inventory")))
        )""",
        """        availability = _to_int(_first(row, ("availability", "availableCount", "inventory")))""",
    ),
    Case(
        "M5 an unpriced room becomes a zero-priced offer",
        HOTELS,
        """        if price is None:
            # No amount means no offer. An unpriced "offer" would enter the budget as a
            # commercial fact with a zero in it.
            return None""",
        """        if price is None:
            # No amount means no offer. An unpriced "offer" would enter the budget as a
            # commercial fact with a zero in it.
            price = Decimal("0")""",
    ),
    Case(
        "M6 the reference carries our id instead of the supplier's",
        HOTELS,
        """        reference = self._reference(
            str(source_id),
            request_context={"tool": "searchHotels", "payload_key": matched_key},
        )""",
        """        reference = self._reference(
            str(stable_id(f"rollinggo:hotel:{source_id}")),
            request_context={"tool": "searchHotels", "payload_key": matched_key},
        )""",
    ),
    Case(
        "M7 the matched payload key is no longer recorded",
        HOTELS,
        '            request_context={"tool": "searchHotels", "payload_key": matched_key},',
        '            request_context={"tool": "searchHotels"},',
    ),
    Case(
        "M8 a hotel with no coordinates is placed at (0.0, 0.0)",
        HOTELS,
        "        if source_id is None or not name or coordinates is None:",
        """        if coordinates is None:
            coordinates = Coordinates(latitude=0.0, longitude=0.0)
        if source_id is None or not name:""",
    ),
    Case(
        "M9 a rejected key is reported as a generic failure",
        CLIENT,
        """        if "status 401" in text or "status 403" in text:""",
        """        if "status 401" in text and "status 403" in text:""",
    ),
    Case(
        "M10 the Accept header drops text/event-stream",
        CLIENT,
        '                    "Accept": MCP_ACCEPT,',
        '                    "Accept": "application/json",',
    ),
]

if __name__ == "__main__":
    raise SystemExit(run_harness(files=(HOTELS, CLIENT), cases=CASES, test_targets=TESTS))
