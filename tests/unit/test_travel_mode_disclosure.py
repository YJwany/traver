"""When the traveller asks for a mode nobody can serve, the plan must say so.

Choosing 公交 / 地铁 with no 高德 key is a supported path, not an error: the free OSRM
providers decline transit rather than substituting a car time, and the itinerary's travel
times become distance-based estimates. Those estimates are already disclosed — as a count,
"N leg(s) use distance-based travel estimates".

The count is not enough. It does not connect the fallback to the request that caused it, so
a traveller who explicitly picked 公交/地铁 sees estimated minutes with no explanation, and
the most natural reading is that the numbers were measured. That is the same
"disclosure without a reason" shape the capability matrix was fixed for: a gap is only
actionable if it says what to do about it.

The other direction matters just as much. A warning that appears on every plan —
  including one where the mode *was* served — trains the reader to skip it, so the tests
  below pin both that it fires when it should and that it stays silent when it should not.
"""

from __future__ import annotations

from datetime import date

from travel_api.domain.models import (
    TRAVEL_MODE_LABELS,
    Itinerary,
    Pace,
    TravelMode,
    TripProfile,
)
from travel_api.services.aggregation import _disclose_unserved_travel_mode


def _profile(*, pace: Pace = Pace.BALANCED, travel_mode: TravelMode | None = None) -> TripProfile:
    return TripProfile(
        destination="杭州",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        pace=pace,
        travel_mode=travel_mode,
    )


def _disclose(profile: TripProfile, routes: list):
    itinerary = Itinerary(days=[], conflicts=[])
    _disclose_unserved_travel_mode(itinerary, profile, routes)
    return itinerary.conflicts


def test_a_requested_transit_mode_with_no_route_source_is_explained():
    """The case the traveller actually hits: they asked for buses, nothing can route them."""
    conflicts = _disclose(_profile(travel_mode=TravelMode.TRANSIT), routes=[])

    assert len(conflicts) == 1
    note = conflicts[0]
    # What was asked for, named the way the interface names it.
    assert TRAVEL_MODE_LABELS["transit"] in note
    # What actually happened.
    assert "估算" in note
    # And what would change it, because this gap is one credential away from closing.
    assert "高德" in note
    # Not overstated: nothing here claims the trip is unplannable.
    assert "无法规划" not in note


def test_the_pace_derived_mode_needs_no_explanation():
    """Deriving walking from a relaxed pace is not a request, so nothing went unserved.

    Warning about it would put a caveat on the default zero-configuration plan, which is
    the one path that must look clean — it is what a user gets before configuring
    anything.
    """
    assert _disclose(_profile(pace=Pace.RELAXED), routes=[]) == []
    assert _disclose(_profile(pace=Pace.BALANCED), routes=[]) == []


def test_the_explanation_does_not_appear_when_the_mode_was_served():
    """A note shown always is a note read never."""
    conflicts = _disclose(_profile(travel_mode=TravelMode.TRANSIT), routes=[object()])
    assert conflicts == []


def test_an_explicit_walk_request_is_treated_like_any_other():
    """Because it *is* a request: it must be held to the same standard as transit.

    Otherwise asking to walk on a deployment with no routing source would produce
    estimated minutes with no explanation, while asking to take the bus would not.
    """
    conflicts = _disclose(_profile(travel_mode=TravelMode.WALK), routes=[])
    assert len(conflicts) == 1
    assert TRAVEL_MODE_LABELS["walk"] in conflicts[0]


def test_the_pace_derived_drive_mode_is_also_silent_without_a_request():
    """A `full` pace derives driving, and that is still not an explicit request."""
    assert _disclose(_profile(pace=Pace.FULL), routes=[]) == []


def test_every_travel_mode_has_a_name_for_the_prose():
    """An unnamed mode would print its English identifier inside a Chinese sentence.

    Asserted over the enum rather than a list, so adding a member without deciding how to
    name it fails here instead of reaching a traveller as `travel_mode: bus`.
    """
    assert set(TRAVEL_MODE_LABELS) == {mode.value for mode in TravelMode}
    assert all(label.strip() for label in TRAVEL_MODE_LABELS.values())


def test_each_mode_is_explained_in_its_own_words():
    """Two modes must not share a note, or the note cannot be acted on."""
    notes = {}
    for mode in TravelMode:
        conflicts = _disclose(_profile(travel_mode=mode), routes=[])
        assert len(conflicts) == 1, f"{mode.value} produced no explanation"
        notes[mode.value] = conflicts[0]

    assert len(set(notes.values())) == len(notes), f"notes are not distinct: {notes}"


def test_the_remedy_matches_the_mode_that_was_requested():
    """A wrong remedy is worse than none: it sends someone to configure a credential
    that would not have helped.

    Asking to walk and being told 公交/地铁 needs a 高德 key is exactly that — the free
    routing tier already serves a real walking profile, so the advice points away from the
    actual cause.
    """
    walk_note = _disclose(_profile(travel_mode=TravelMode.WALK), routes=[])[0]
    transit_note = _disclose(_profile(travel_mode=TravelMode.TRANSIT), routes=[])[0]

    # Only transit's remedy should send the reader to a credential.
    assert "高德" in transit_note
    assert "高德" not in walk_note
    # And walking's should point at the free tier that actually serves it.
    assert "OSRM" in walk_note or "免费" in walk_note


def test_driving_and_taxi_share_a_remedy_but_say_which_mode_was_asked_for():
    """They are routed the same way, so the fix is the same — but the note still has to
    name the request, or it reads as a comment about something the reader did not do."""
    drive_note = _disclose(_profile(travel_mode=TravelMode.DRIVE), routes=[])[0]
    taxi_note = _disclose(_profile(travel_mode=TravelMode.TAXI), routes=[])[0]

    assert TRAVEL_MODE_LABELS["drive"] in drive_note
    assert TRAVEL_MODE_LABELS["taxi"] in taxi_note
    assert drive_note != taxi_note


def test_the_explanation_is_added_without_losing_existing_conflicts():
    """It appends to the itinerary's own findings rather than replacing them."""
    itinerary = Itinerary(days=[], conflicts=["测试景点: opening hours do not match"])
    _disclose_unserved_travel_mode(itinerary, _profile(travel_mode=TravelMode.TRANSIT), [])

    assert itinerary.conflicts[0] == "测试景点: opening hours do not match"
    assert len(itinerary.conflicts) == 2


def test_a_plan_with_no_travel_at_all_is_still_told_its_mode_was_unserved():
    """The note follows the *request*, not the number of legs.

    A trip whose points could not be routed and a trip with too few points to route both
    leave `routes` empty, and in both the traveller's request went unserved. Keying the
    note off a leg count instead would make it disappear on exactly the plans where the
    fallback was most complete.
    """
    conflicts = _disclose(_profile(travel_mode=TravelMode.TRANSIT), routes=[])
    assert conflicts, "an unserved request must be disclosed even with no legs at all"
