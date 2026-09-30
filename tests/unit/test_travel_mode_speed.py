"""Estimated travel minutes must follow the mode asked for, not the pace.

An itinerary has two independent settings, and they were wired to each other:

  * ``pace`` — how densely to pack a day. RELAXED / BALANCED / FULL.
  * ``travel_mode`` — how the traveller moves. walk / transit / taxi / drive.

When the routing provider has no measured leg for a pair of places, the optimizer estimates one
from the straight-line distance. The speed it divided by came from ``pace``: RELAXED and BALANCED
meant walking, FULL meant driving. ``travel_mode`` was never read, which made this a *third*
derivation of the mode — ``resolve_travel_mode`` in ``providers/base.py`` exists precisely so the
two routing providers cannot disagree about it, and warns that deriving it in two places lets
them drift.

Reproduced on a ~13 km hop in 成都, before the fix:

==================  ==============  ==========  ==============================
request             old minutes     old speed   what that speed means
==================  ==============  ==========  ==============================
FULL + walk         44              18.2 km/h   jogging, for 13 km
RELAXED + drive     231             3.5 km/h    crawling, in a car
==================  ==============  ==========  ==============================

Both directions are wrong, and neither is disclosed: the itinerary said the minutes were
「按距离估算」 without naming the speed, so a reader could not tell a plausible estimate from an
absurd one. These tests pin the mode, the disclosure, and the pace-derived default that must not
change.

The last point matters more than it looks. ``resolve_travel_mode`` maps an unset mode to walk for
RELAXED/BALANCED and drive for FULL, so every trip planned before ``travel_mode`` existed keeps
exactly the route it had. A fix that changed that would silently rewrite stored plans.
"""

from __future__ import annotations

from datetime import date

import pytest
from travel_api.domain.geo import straight_line_meters
from travel_api.domain.models import Pace, TravelMode, TripProfile
from travel_api.providers.base import resolve_travel_mode
from travel_api.services.route_optimizer import (
    _ASSUMED_SPEED_BY_MODE,
    _DRIVING_METERS_PER_MINUTE,
    _TRANSIT_METERS_PER_MINUTE,
    _WALKING_METERS_PER_MINUTE,
    RouteOptimizer,
    _derive_minutes,
    assumed_speed_for,
)

#: Roughly the 成都 city centre to its northern edge — long enough that the choice of speed
#: dominates the rounding, short enough to be an ordinary hop.
HOP_METERS = straight_line_meters((30.6574, 104.0817), (30.7500, 104.1700))


def _profile(pace: Pace, mode: TravelMode | None) -> TripProfile:
    return TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        pace=pace,
        travelers=2,
        travel_mode=mode,
    )


def _minutes(pace: Pace, mode: TravelMode | None) -> int:
    return _derive_minutes(HOP_METERS, _profile(pace, mode))


# -- the defect ----------------------------------------------------------------


def test_an_explicit_walking_request_is_not_timed_as_a_car():
    """The FAILING-first case: FULL used to mean *driving*, whatever was asked for.

    On a 13 km hop the old code returned 44 minutes for ``FULL + walk`` — an implied 18 km/h on
    foot, which is a run, not a walk.
    """
    walking = _minutes(Pace.FULL, TravelMode.WALK)
    driving = _minutes(Pace.FULL, TravelMode.DRIVE)
    assert walking > driving, (
        f"walking ({walking} min) is not slower than driving ({driving} min) on a "
        f"{HOP_METERS / 1000:.1f} km hop, so the estimate is ignoring travel_mode"
    )
    implied_kmh = (HOP_METERS / 1000) / (walking / 60)
    assert implied_kmh < 8, (
        f"a walking estimate implies {implied_kmh:.1f} km/h, which nobody sustains on foot"
    )


def test_an_explicit_driving_request_is_not_timed_on_foot():
    """The other direction of the same defect: RELAXED + drive used to imply 3.5 km/h."""
    driving = _minutes(Pace.RELAXED, TravelMode.DRIVE)
    implied_kmh = (HOP_METERS / 1000) / (driving / 60)
    assert implied_kmh > 10, (
        f"a driving estimate implies {implied_kmh:.1f} km/h, which is slower than cycling — the "
        "speed is coming from the pace, not the mode"
    )


def test_pace_no_longer_changes_the_speed_of_a_given_mode():
    """The two settings are independent, so one must not move the other.

    This is the cleanest statement of the defect and it needs no threshold and no knowledge of
    the constants: whatever speed the planner assumes for a mode, it must assume the same speed
    for that mode at every pace.
    """
    for mode in (TravelMode.WALK, TravelMode.TRANSIT, TravelMode.TAXI, TravelMode.DRIVE):
        minutes = {pace: _minutes(pace, mode) for pace in Pace}
        assert len(set(minutes.values())) == 1, (
            f"mode={mode.value} is timed differently depending on pace: "
            f"{ {p.value: m for p, m in minutes.items()} } — the speed still follows the pace"
        )


# -- the speeds are the ones the labels claim ----------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (TravelMode.WALK, _WALKING_METERS_PER_MINUTE),
        (TravelMode.TRANSIT, _TRANSIT_METERS_PER_MINUTE),
        (TravelMode.DRIVE, _DRIVING_METERS_PER_MINUTE),
        (TravelMode.TAXI, _DRIVING_METERS_PER_MINUTE),
    ],
)
def test_each_mode_is_timed_at_its_own_assumed_speed(mode, expected):
    speed, _ = assumed_speed_for(_profile(Pace.BALANCED, mode))
    assert speed == expected


def test_transit_is_slower_than_driving_and_faster_than_walking():
    """Ordering, not values — because the *consequence* of getting it wrong is a bad plan.

    A transit estimate below the walking figure would make the optimizer prefer to send someone
    across a city on foot, which is the failure mode worth preventing rather than the exact
    constant.
    """
    walk, _ = assumed_speed_for(_profile(Pace.BALANCED, TravelMode.WALK))
    transit, _ = assumed_speed_for(_profile(Pace.BALANCED, TravelMode.TRANSIT))
    drive, _ = assumed_speed_for(_profile(Pace.BALANCED, TravelMode.DRIVE))
    assert walk < transit < drive


def test_taxi_is_assumed_to_be_no_faster_than_driving():
    """A taxi moves through the same traffic. Assuming otherwise would favour calling one."""
    taxi, _ = assumed_speed_for(_profile(Pace.BALANCED, TravelMode.TAXI))
    drive, _ = assumed_speed_for(_profile(Pace.BALANCED, TravelMode.DRIVE))
    assert taxi == drive


# -- the disclosure ------------------------------------------------------------


def test_the_disclosure_names_the_speed_and_matches_the_arithmetic():
    """「按距离估算」 is honest but not checkable. The speed makes it checkable.

    The text and the number are returned together from one function, so this also pins that they
    cannot describe different modes — the failure that would matter most, since the sentence
    exists to explain the number.
    """
    for mode in (TravelMode.WALK, TravelMode.TRANSIT, TravelMode.TAXI, TravelMode.DRIVE):
        speed, text = assumed_speed_for(_profile(Pace.BALANCED, mode))
        assert text, f"mode={mode.value} has no disclosure text"
        # The assumed speed is written in Chinese units, so the comparison is against the number
        # the arithmetic produces rather than a Latin unit string. See `_ASSUMED_SPEED_TEXT` for
        # why it is not `km/h`.
        kmh = speed * 60 / 1000
        assert f"{kmh:g}" in text, (
            f"mode={mode.value} is timed at {kmh:g} 公里/小时 but discloses {text!r}"
        )
        assert f"{kmh:g} km/h" not in text, (
            f"mode={mode.value} discloses a Latin unit in otherwise Chinese prose: {text!r}"
        )


def test_every_travel_mode_has_an_assumed_speed():
    """A mode with no entry would fall back to the driving default, silently.

    ``_ASSUMED_SPEED_BY_MODE.get`` has a default, so a new ``TravelMode`` member compiles and
    plans at car speed unless something checks. This is that check.
    """
    missing = [mode for mode in TravelMode if mode not in _ASSUMED_SPEED_BY_MODE]
    assert not missing, (
        f"{[m.value for m in missing]} have no assumed speed and would silently be estimated at "
        "the driving speed"
    )


# -- the sentence the user actually reads --------------------------------------


def test_the_itinerary_conflict_names_the_speed_it_used():
    """The disclosure has to reach the plan, not just exist as a function.

    ``assumed_speed_for`` returning the right text proves nothing on its own: the old code
    already told the traveller these minutes were 按距离估算, and that sentence was true and
    useless — a reader cannot tell a plausible estimate from an absurd one without the speed.
    The first version of this fix returned the text and did not print it, which is why this
    test drives the optimizer and reads its conflicts rather than calling the helper.

    It goes through the real public entry point with two attractions whose coordinates are known
    and no route legs supplied, so every leg takes the estimation path.
    """
    from travel_api.domain.models import Attraction, Coordinates, Location

    def attraction(name: str, latitude: float, longitude: float) -> Attraction:
        return Attraction(
            canonical_name=name,
            location=Location(
                city="成都",
                address="武侯区",
                coordinates=Coordinates(latitude=latitude, longitude=longitude),
            ),
            recommended_duration_minutes=90,
        )

    profile = _profile(Pace.FULL, TravelMode.WALK)
    itinerary = RouteOptimizer().optimize(
        profile,
        None,
        None,
        [attraction("武侯祠", 30.6495, 104.0432), attraction("锦里", 30.6490, 104.0400)],
        [],
        [],
    )

    estimated = [text for text in itinerary.conflicts if "按距离估算" in text]
    assert estimated, (
        f"no conflict reports an estimated leg, so the disclosure never reached the plan: "
        f"{itinerary.conflicts}"
    )
    assert any("公里" in text for text in estimated), (
        f"the estimate is disclosed without naming the assumed speed: {estimated}. The sentence "
        "「按距离估算」 is true and unhelpful — it is what let an 18 km/h walking estimate ship."
    )
    assert any("步行" in text for text in estimated), (
        f"a walking trip disclosed a speed that is not walking: {estimated}"
    )


# -- the pace-derived default must not change ----------------------------------


@pytest.mark.parametrize(
    ("pace", "expected"),
    [
        (Pace.RELAXED, TravelMode.WALK),
        (Pace.BALANCED, TravelMode.WALK),
        (Pace.FULL, TravelMode.DRIVE),
    ],
)
def test_an_unset_mode_still_derives_from_pace_exactly_as_before(pace, expected):
    """Stored plans must keep their routes.

    Trips planned before ``travel_mode`` existed have it unset, and their itineraries are in the
    database. A fix that changed what an unset mode resolves to would rewrite every one of them
    on the next revision without anyone asking.
    """
    assert resolve_travel_mode(_profile(pace, None)) is expected


def test_a_pace_only_trip_is_timed_exactly_as_it_was_before_the_fix():
    """The regression guard for the default path, stated as behaviour.

    RELAXED and BALANCED resolve to walk, FULL to drive, and those speeds are unchanged — so
    these four numbers are what the old code produced. If a later change to the mode handling
    moves them, the plan for every trip with no explicit mode moves with it.
    """
    assert _minutes(Pace.RELAXED, None) == _minutes(Pace.RELAXED, TravelMode.WALK)
    assert _minutes(Pace.BALANCED, None) == _minutes(Pace.BALANCED, TravelMode.WALK)
    assert _minutes(Pace.FULL, None) == _minutes(Pace.FULL, TravelMode.DRIVE)
