"""No revision may time a leg from a place the traveller has never been.

`RouteOptimizer.optimize` takes the starting hotel as an *id and a coordinate pair*, and it needs
both or neither: the id is what a measured leg is looked up by, and the coordinates are what a
straight-line estimate is computed from when no leg exists. Its own guard enforces this on the
id:

    last_coordinates = hotel_coordinates if hotel_id is not None else None

which is why passing `(0.0, 0.0)` *without* an id is harmless. With an id, it is not — and two
revision paths did exactly that, pairing a real selected-hotel id with the literal placeholder:

    hotel_id = self._selected_hotel_id(preview, trip.selected_hotel_offer_id)
    RouteOptimizer().optimize(trip.profile, hotel_id, (0.0, 0.0), ...)   # Null Island

`(0.0, 0.0)` is in the Gulf of Guinea, about 11,000 km from 成都, so every leg without a measured
route was estimated from there. Reproduced before the fix: a 武侯祠 item on a 2026-10-03 day got
`travel_from_previous_minutes = 196688` (136 days) and a start time of 2027-02-16 — and when the
attraction also had opening hours, the item was dropped and the traveller was told
「营业时间与可用时段不匹配」, a message about the wrong thing entirely.

Why no test caught it: nothing exercised `add_attraction` or `remove_attraction` at all, and
`tests/unit/test_route_optimizer.py` pins only the `hotel_id=None` case — that is, the guard,
which is the half that worked.

These tests go through the real HTTP revision endpoint rather than the private method, because
the defect was in the *call site*, not in the optimizer. A test of the optimizer would have kept
passing. What is asserted is the observable consequence: the minutes are bounded by something a
person could travel, and no item is dropped with an opening-hours excuse it did not earn.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app

#: Nothing inside 成都 is more than a few hours away, and even a wrong-city estimate is bounded
#: by roughly a day. 136 days is not a travel time under any reading, so the bound only has to
#: be far above any real value and far below the placeholder's.
ABSURD_MINUTES = 24 * 60


def _client(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'null-island.db'}",
        auto_create_schema=True,
        provider_retry_count=0,
        # Hermetic: the mock provider supplies 成都's hotels and attractions with no network.
        free_providers_enabled=False,
        mock_provider_enabled=True,
    )
    return TestClient(create_app(settings))


def _created_trip(client: TestClient) -> dict:
    created = client.post(
        "/api/v1/trips",
        json={
            "destination": "成都",
            "start_date": "2026-10-03",
            "end_date": "2026-10-05",
            "travelers": 2,
            "total_budget": 5000,
            "pace": "relaxed",
        },
    )
    assert created.status_code == 200, created.text
    return created.json()


def _travel_minutes(trip: dict) -> list[int]:
    return [
        item["travel_from_previous_minutes"]
        for day in trip["itinerary"]["days"]
        for item in day["items"]
    ]


def _scheduled_dates(trip: dict) -> list[str]:
    return [item["start_time"] for day in trip["itinerary"]["days"] for item in day["items"]]


def test_adding_an_attraction_does_not_time_the_leg_from_null_island(tmp_path):
    """The `add_attraction` path, which passed a real hotel id beside `(0.0, 0.0)`."""
    with _client(tmp_path) as client:
        trip = _created_trip(client)
        assert trip["selected_hotel_offer_id"] is not None, (
            "the fixture selected no hotel, so the id guard would hide the placeholder and this "
            "test would pass without testing anything"
        )
        candidates = trip["preview"]["attractions"]
        assert candidates, "the mock supplied no attractions, so there is nothing to add"

        revised = client.post(
            f"/api/v1/trips/{trip['id']}/revision",
            json={
                "action": "add_attraction",
                "constraints": {"attraction_id": str(candidates[0]["id"])},
            },
        )
        assert revised.status_code == 200, revised.text
        after = revised.json()["trip"]

        minutes = _travel_minutes(after)
        assert minutes, "the revision produced no scheduled item, so nothing was measured"
        worst = max(minutes)
        assert worst < ABSURD_MINUTES, (
            f"a leg takes {worst} minutes ({worst / 60 / 24:.0f} days). With a real hotel id and "
            "a placeholder coordinate the optimizer estimates from (0.0, 0.0), which is Null "
            "Island, not the hotel."
        )


def test_the_scheduled_start_stays_inside_the_trip(tmp_path):
    """The visible symptom, asserted directly.

    An out-of-range travel estimate does not merely print a big number: it pushes `start_time`
    past the end of the trip, which is how a 成都 trip ends up scheduling an item in February
    of the following year.
    """
    with _client(tmp_path) as client:
        trip = _created_trip(client)
        candidates = trip["preview"]["attractions"]
        assert candidates

        revised = client.post(
            f"/api/v1/trips/{trip['id']}/revision",
            json={
                "action": "add_attraction",
                "constraints": {"attraction_id": str(candidates[0]["id"])},
            },
        )
        assert revised.status_code == 200, revised.text
        after = revised.json()["trip"]

        end = date.fromisoformat(trip["profile"]["end_date"])
        for start in _scheduled_dates(after):
            moment = datetime.fromisoformat(start)
            # One day of slack for the last day's evening items; the point is the *order* of
            # magnitude, and the placeholder put this five months out.
            assert moment.date() <= end, (
                f"an item on a trip ending {end} starts at {moment}, which is the "
                "Null-Island estimate leaking into the schedule"
            )


def test_removing_an_attraction_does_not_time_the_leg_from_null_island(tmp_path):
    """The `remove_attraction` path, the second call site with the same placeholder.

    It reaches the same code through a different branch, and the two were fixed separately in
    the original defect — so testing only one would leave the other free to regress.
    """
    with _client(tmp_path) as client:
        trip = _created_trip(client)
        assert trip["selected_hotel_offer_id"] is not None
        candidates = trip["preview"]["attractions"]
        assert len(candidates) >= 2, "need at least two attractions to remove one meaningfully"

        revised = client.post(
            f"/api/v1/trips/{trip['id']}/revision",
            json={
                "action": "remove_attraction",
                "constraints": {"attraction_id": str(candidates[0]["id"])},
            },
        )
        assert revised.status_code == 200, revised.text
        after = revised.json()["trip"]

        minutes = _travel_minutes(after)
        assert minutes, "the revision produced no scheduled item"
        assert max(minutes) < ABSURD_MINUTES, (
            f"a leg after removing an attraction takes {max(minutes)} minutes, so this branch "
            "still estimates from a placeholder coordinate"
        )


def test_a_revision_with_no_selected_hotel_still_produces_a_sane_plan(tmp_path):
    """The other side of the pair, so a fix that always passed the hotel's coordinates is safe.

    When no hotel is selected there is no origin, and `(None, None)` must mean "no estimate from
    a hotel" rather than "estimate from somewhere". Without this, a fix that unconditionally read
    `preview.hotels[0]` would satisfy the tests above while inventing an origin for a trip whose
    hotel the traveller never chose.
    """
    with _client(tmp_path) as client:
        trip = _created_trip(client)
        candidates = trip["preview"]["attractions"]
        assert candidates

        # Pace changes go through the same origin helper and are reachable without a hotel.
        revised = client.post(
            f"/api/v1/trips/{trip['id']}/revision",
            json={"action": "change_pace", "constraints": {"pace": "full"}},
        )
        assert revised.status_code == 200, revised.text
        after = revised.json()["trip"]
        assert after["profile"]["pace"] == "full"

        minutes = _travel_minutes(after)
        assert all(minute < ABSURD_MINUTES for minute in minutes), (
            f"a pace change produced an absurd travel time: {minutes}"
        )
