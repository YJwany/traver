from datetime import date, time
from uuid import uuid4

from travel_api.domain.models import (
    Attraction,
    Coordinates,
    Location,
    OpeningHours,
    Pace,
    TripProfile,
)
from travel_api.services.route_optimizer import RouteOptimizer

#: Hotel anchor used by the tests.
HOTEL_COORDINATES = (30.65, 104.08)


def _attraction(
    opens_at: time, closes_at: time, coordinates: Coordinates | None = None
) -> Attraction:
    return Attraction(
        canonical_name="测试景点",
        location=Location(
            address="测试地址",
            city="成都",
            coordinates=coordinates or Coordinates(latitude=30.65, longitude=104.08),
        ),
        recommended_duration_minutes=120,
        opening_hours=[
            OpeningHours(weekday=weekday, opens_at=opens_at, closes_at=closes_at)
            for weekday in range(7)
        ],
    )


def _profile(pace: Pace = Pace.RELAXED) -> TripProfile:
    return TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 4),
        pace=pace,
    )


def test_route_optimizer_rejects_items_that_do_not_fit_opening_hours():
    itinerary = RouteOptimizer().optimize(
        _profile(), uuid4(), HOTEL_COORDINATES, [_attraction(time(9), time(10))], [], []
    )

    assert itinerary.days[0].items == []
    # The attraction does have a window on a trip weekday, just not one that fits.
    assert itinerary.conflicts == ["测试景点: opening hours do not match any available slot"]


def test_leftover_attraction_blames_capacity_not_opening_hours():
    """More always-open attractions than the trip has slots for.

    Previously every leftover attraction was reported as an opening-hours problem,
    which sent users looking in the wrong place when the real cause was capacity.
    This mirrors the live case: dozens of OSM attractions on a three-day trip.
    """
    attractions = [
        Attraction(
            canonical_name=f"景点{index}",
            location=Location(
                address="测试地址",
                city="成都",
                coordinates=Coordinates(latitude=30.65 + index * 0.001, longitude=104.08),
            ),
            recommended_duration_minutes=60,
        )
        for index in range(5)
    ]
    itinerary = RouteOptimizer().optimize(
        _profile(Pace.RELAXED), uuid4(), HOTEL_COORDINATES, attractions, [], []
    )

    # One relaxation of a one-day trip fits a single attraction.
    assert len(itinerary.days[0].items) == 1
    leftovers = [c for c in itinerary.conflicts if "not enough trip days" in c]
    assert len(leftovers) == 4
    assert not any("opening hours" in conflict for conflict in itinerary.conflicts)


def test_travel_time_is_derived_from_distance_not_a_constant():
    """A hop with no routing leg must use the two coordinates, not a fixed number.

    The old implementation always returned the same pace-based constant (21
    minutes for RELAXED) regardless of where the places actually were, which is a
    fabricated travel time rather than an estimate of anything.
    """
    origin = Coordinates(latitude=30.65, longitude=104.08)
    # ~1.47 km straight line; ~1.9 km by road.
    destination = Coordinates(latitude=30.66, longitude=104.09)
    itinerary = RouteOptimizer().optimize(
        _profile(Pace.RELAXED),
        uuid4(),
        (origin.latitude, origin.longitude),
        [_attraction(time(9), time(22), destination)],
        [],
        [],
    )
    item = itinerary.days[0].items[0]

    # 1.9 km at 4.5 km/h.
    assert item.travel_from_previous_minutes == 26
    assert item.travel_from_previous_minutes != 21  # the old constant
    assert any("distance-based travel estimates" in conflict for conflict in itinerary.conflicts)


def test_travel_speed_follows_pace():
    """The same hop is slower on a relaxed trip than on a full one."""
    origin = Coordinates(latitude=30.65, longitude=104.08)
    destination = Coordinates(latitude=30.66, longitude=104.09)
    attractions = [_attraction(time(9), time(22), destination)]

    relaxed = (
        RouteOptimizer()
        .optimize(
            _profile(Pace.RELAXED),
            uuid4(),
            (origin.latitude, origin.longitude),
            list(attractions),
            [],
            [],
        )
        .days[0]
        .items[0]
    )
    full = (
        RouteOptimizer()
        .optimize(
            _profile(Pace.FULL),
            uuid4(),
            (origin.latitude, origin.longitude),
            list(attractions),
            [],
            [],
        )
        .days[0]
        .items[0]
    )

    assert relaxed.travel_from_previous_minutes == 26  # walking
    assert full.travel_from_previous_minutes == 5  # ~1.9 km at 24 km/h
    assert relaxed.travel_from_previous_minutes > full.travel_from_previous_minutes


def test_zero_distance_hop_is_not_inflated():
    """Two places at the same coordinates must not report a cross-town trip."""
    itinerary = RouteOptimizer().optimize(
        _profile(), uuid4(), HOTEL_COORDINATES, [_attraction(time(9), time(22))], [], []
    )
    assert itinerary.days[0].items[0].travel_from_previous_minutes == 1


def test_nearest_attraction_is_scheduled_first():
    """The optimizer must prefer the closest feasible attraction.

    Taking the first list entry produced itineraries that walked several
    kilometres past nearer options; with real routing data available there is no
    reason to ignore distance.
    """
    hotel_id = uuid4()
    hotel_coordinates = (30.6500, 104.0800)
    near = Attraction(
        canonical_name="近处景点",
        location=Location(
            address="测试地址",
            city="成都",
            coordinates=Coordinates(latitude=30.6510, longitude=104.0810),
        ),
        recommended_duration_minutes=60,
    )
    far = Attraction(
        canonical_name="远处景点",
        location=Location(
            address="测试地址",
            city="成都",
            coordinates=Coordinates(latitude=30.6950, longitude=104.0800),
        ),
        recommended_duration_minutes=60,
    )

    itinerary = RouteOptimizer().optimize(
        _profile(Pace.FULL),
        hotel_id,
        hotel_coordinates,
        # Deliberately list the far attraction first.
        [far, near],
        [],
        [],
    )

    first = itinerary.days[0].items[0]
    assert first.title == "近处景点"
    assert first.travel_from_previous_minutes < 10


def test_measured_route_legs_are_not_flagged_as_estimates():
    """A real measured leg must be used verbatim and not disclosed as an estimate."""
    from datetime import datetime, timezone

    from travel_api.domain.models import (
        DataDomain,
        RouteLeg,
        SourceReference,
        SourceType,
        TravelMode,
    )

    attraction = _attraction(time(9), time(22))
    hotel_id = uuid4()
    now = datetime.now(timezone.utc)
    reference = SourceReference(
        source="osrm",
        source_type=SourceType.OPEN_DATA,
        source_id="osrm:driving:2",
        observed_at=now,
        fetched_at=now,
        expires_at=now,
        domain=DataDomain.GEO_FACT,
    )
    routes = [
        RouteLeg(
            origin_id=hotel_id,
            destination_id=attraction.id,
            mode=TravelMode.WALK,
            distance_meters=2000,
            duration_minutes=27,
            is_estimate=False,
            source_reference=reference,
        )
    ]
    itinerary = RouteOptimizer().optimize(
        _profile(), hotel_id, HOTEL_COORDINATES, [attraction], [], routes
    )
    assert itinerary.days[0].items[0].travel_from_previous_minutes == 27
    assert not any("distance-based" in conflict for conflict in itinerary.conflicts)
