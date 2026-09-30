from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import ceil

from travel_api.domain.geo import straight_line_meters
from travel_api.domain.models import (
    Attraction,
    Itinerary,
    ItineraryDay,
    ItineraryItem,
    ItineraryItemType,
    Pace,
    Restaurant,
    RouteLeg,
    TravelMode,
    TripProfile,
)
from travel_api.providers.base import resolve_travel_mode

#: Assumed urban speeds used only when the routing provider has no leg for a pair.
#: Straight-line distance under-estimates road distance, so these are deliberately
#: conservative and every leg derived from them is recorded as an estimate.
_WALKING_METERS_PER_MINUTE = 75.0  # ~4.5 km/h
_DRIVING_METERS_PER_MINUTE = 400.0  # ~24 km/h
#: 公交 / 地铁: slower than a car because of stops, boarding and waiting; faster than walking.
#: Kept above the walking figure deliberately — a transit estimate that came out *worse* than
#: walking would make the planner prefer to send someone 10 km on foot.
_TRANSIT_METERS_PER_MINUTE = 250.0  # ~15 km/h
#: Road distance exceeds straight-line distance by roughly this factor in a city.
_ROAD_DETOUR_FACTOR = 1.3


class RouteOptimizer:
    pace_buffers = {
        Pace.RELAXED: 20,
        Pace.BALANCED: 10,
        Pace.FULL: 5,
    }

    def optimize(
        self,
        profile: TripProfile,
        hotel_id,
        hotel_coordinates: tuple[float, float] | None,
        attractions: list[Attraction],
        restaurants: list[Restaurant],
        routes: list[RouteLeg],
    ) -> Itinerary:
        attraction_queue = list(attractions)
        restaurant_queue = list(restaurants)
        days: list[ItineraryDay] = []
        conflicts: list[str] = []
        buffer_minutes = self.pace_buffers[profile.pace]
        attractions_per_day = 1 if profile.pace is Pace.RELAXED else 2

        # Coordinates let the optimizer fall back to a distance-based estimate
        # instead of a flat constant when the routing provider has no leg.
        coordinates = _coordinate_index(attractions, restaurants)
        estimated_legs = 0

        for day_index in range(profile.nights):
            day_date = profile.start_date + timedelta(days=day_index)
            day = ItineraryDay(day_date=day_date, day_index=day_index + 1)
            cursor = datetime.combine(day_date, time(9, 30))
            sequence = 1
            last_entity_id = hotel_id
            # Both the id and the coordinates, or neither. The id is what a routing leg is
            # looked up by; the coordinates are what a distance estimate needs. Starting from
            # coordinates alone would estimate travel from a place that is not in the
            # itinerary, which is why this is not simply `hotel_coordinates`.
            last_coordinates = hotel_coordinates if hotel_id is not None else None

            for _ in range(attractions_per_day):
                candidate = self._next_open_attraction(
                    attraction_queue,
                    day_date,
                    cursor,
                    profile,
                    last_entity_id,
                    last_coordinates,
                    routes,
                    coordinates,
                )
                if candidate is None:
                    break
                attraction, travel_minutes, travel_estimated = candidate
                start = cursor + timedelta(minutes=travel_minutes)
                end = start + timedelta(minutes=attraction.recommended_duration_minutes)
                day.items.append(
                    ItineraryItem(
                        sequence=sequence,
                        item_type=ItineraryItemType.ATTRACTION,
                        entity_id=attraction.id,
                        title=attraction.canonical_name,
                        start_time=start,
                        end_time=end,
                        travel_from_previous_minutes=travel_minutes,
                        visit_duration_minutes=attraction.recommended_duration_minutes,
                    )
                )
                sequence += 1
                cursor = end + timedelta(minutes=buffer_minutes)
                last_entity_id = attraction.id
                last_coordinates = _coordinates_of(attraction)
                estimated_legs += int(travel_estimated)
                attraction_queue.remove(attraction)

            lunch = self._next_restaurant(restaurant_queue)
            if lunch and cursor < datetime.combine(day_date, time(14, 0)):
                travel_minutes, travel_estimated = self._travel(
                    last_entity_id,
                    last_coordinates,
                    lunch,
                    routes,
                    profile,
                    coordinates,
                )
                start = max(
                    cursor + timedelta(minutes=travel_minutes),
                    datetime.combine(day_date, time(12, 0)),
                )
                end = start + timedelta(minutes=60)
                if end <= datetime.combine(day_date, time(14, 0)):
                    day.items.append(
                        ItineraryItem(
                            sequence=sequence,
                            item_type=ItineraryItemType.RESTAURANT,
                            entity_id=lunch.id,
                            title=lunch.canonical_name,
                            start_time=start,
                            end_time=end,
                            travel_from_previous_minutes=travel_minutes,
                            visit_duration_minutes=60,
                        )
                    )
                    sequence += 1
                    cursor = end + timedelta(minutes=buffer_minutes)
                    last_entity_id = lunch.id
                    last_coordinates = _coordinates_of(lunch)
                    estimated_legs += int(travel_estimated)
                    restaurant_queue.append(restaurant_queue.pop(0))

            dinner = self._next_restaurant(restaurant_queue)
            if dinner:
                travel_minutes, travel_estimated = self._travel(
                    last_entity_id,
                    last_coordinates,
                    dinner,
                    routes,
                    profile,
                    coordinates,
                )
                start = max(
                    cursor + timedelta(minutes=travel_minutes),
                    datetime.combine(day_date, time(18, 0)),
                )
                end = start + timedelta(minutes=90)
                if end <= datetime.combine(day_date, time(22, 0)):
                    day.items.append(
                        ItineraryItem(
                            sequence=sequence,
                            item_type=ItineraryItemType.RESTAURANT,
                            entity_id=dinner.id,
                            title=dinner.canonical_name,
                            start_time=start,
                            end_time=end,
                            travel_from_previous_minutes=travel_minutes,
                            visit_duration_minutes=90,
                        )
                    )
                    cursor = end + timedelta(minutes=buffer_minutes)
                    last_entity_id = dinner.id
                    last_coordinates = _coordinates_of(dinner)
                    estimated_legs += int(travel_estimated)
                    restaurant_queue.append(restaurant_queue.pop(0))

            if not day.items:
                day.warnings.append("no feasible itinerary item for this day")
            days.append(day)

        for attraction in attraction_queue:
            conflicts.append(f"{attraction.canonical_name}: {_leftover_reason(attraction, days)}")
        if estimated_legs:
            # Disclose derived travel times rather than presenting them as measured. Written in
            # Chinese because the server's own prose is Chinese everywhere else — this line
            # reaches the user directly, and it was showing up untranslated in the middle of an
            # otherwise Chinese exported itinerary.
            #
            # The assumed speed is named, not just the fact of estimation. "按距离估算" says the
            # number is not measured, which is honest but not yet checkable: a reader cannot tell
            # a plausible estimate from an absurd one without knowing the speed behind it, and
            # this figure is exactly where an absurd one lived (18 km/h on foot, when the speed
            # followed the pace instead of the mode). Naming it makes a wrong assumption legible
            # in the output instead of only in the code.
            _, speed_text = assumed_speed_for(profile)
            conflicts.append(f"{estimated_legs} 段路程按距离估算（{speed_text}），不是实测路线")
        return Itinerary(days=days, conflicts=conflicts)

    def _next_open_attraction(
        self,
        candidates: list[Attraction],
        day_date: date,
        cursor: datetime,
        profile: TripProfile,
        previous_id,
        previous_coordinates: tuple[float, float] | None,
        routes: list[RouteLeg],
        coordinates: dict,
    ) -> tuple[Attraction, int, bool] | None:
        """Pick the *closest* attraction that still fits, not simply the first one.

        With real routing data available, taking the first list entry produces
        itineraries that walk kilometres past nearer options (live testing paired
        attractions 7.8 km apart while closer ones sat unused). Selecting by
        travel time is what makes this a route optimizer rather than a scheduler.
        """
        best: tuple[Attraction, int, bool] | None = None
        for attraction in candidates:
            travel_minutes, estimated = self._travel(
                previous_id,
                previous_coordinates,
                attraction,
                routes,
                profile,
                coordinates,
            )
            if best is not None and travel_minutes >= best[1]:
                continue
            proposed_start = cursor + timedelta(minutes=travel_minutes)
            proposed_end = proposed_start + timedelta(
                minutes=attraction.recommended_duration_minutes
            )
            if self._fits_opening_hours(
                attraction, day_date, proposed_start.time(), proposed_end.time()
            ):
                best = (attraction, travel_minutes, estimated)
        return best

    @staticmethod
    def _fits_opening_hours(attraction: Attraction, day_date: date, start: time, end: time) -> bool:
        windows = [
            window for window in attraction.opening_hours if window.weekday == day_date.weekday()
        ]
        if not windows:
            return True
        return any(window.opens_at <= start and end <= window.closes_at for window in windows)

    @staticmethod
    def _next_restaurant(restaurants: list[Restaurant]) -> Restaurant | None:
        return restaurants[0] if restaurants else None

    def _travel(
        self,
        previous_id,
        previous_coordinates: tuple[float, float] | None,
        destination,
        routes: list[RouteLeg],
        profile: TripProfile,
        coordinates: dict,
    ) -> tuple[int, bool]:
        """Return (minutes, is_estimate) for one hop.

        Order of preference: a real routing leg, then a distance-based estimate
        from the two coordinates, then a pace-based allowance. Only the first is a
        measurement; the others are flagged so callers can disclose them.
        """
        route = next(
            (
                item
                for item in routes
                if item.origin_id == previous_id and item.destination_id == destination.id
            ),
            None,
        )
        if route is not None and not route.is_estimate:
            return route.duration_minutes, False
        if route is not None:
            return route.duration_minutes, True

        if previous_id is not None and previous_id == destination.id:
            return 0, False

        destination_coordinates = _coordinates_of(destination)
        if previous_coordinates and destination_coordinates:
            metres = _haversine_meters(previous_coordinates, destination_coordinates)
            minutes = _derive_minutes(metres, profile)
            return minutes, True

        return _pace_allowance(profile), True


def _coordinates_of(entity) -> tuple[float, float] | None:
    location = getattr(entity, "location", None)
    coordinates = getattr(location, "coordinates", None)
    if coordinates is None:
        return None
    return (coordinates.latitude, coordinates.longitude)


def _coordinate_index(attractions: list[Attraction], restaurants: list[Restaurant]) -> dict:
    index: dict = {}
    for entity in [*attractions, *restaurants]:
        coordinates = _coordinates_of(entity)
        if coordinates is not None:
            index[entity.id] = coordinates
    return index


def _haversine_meters(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    """Great-circle distance in metres.

    Delegates to ``domain/geo`` so there is one implementation: the place resolver needs the
    same measurement to reject a namesake far from the destination, and two copies of a
    formula is how one of them quietly acquires a different earth radius.
    """
    return straight_line_meters(origin, destination)


#: Metres per minute assumed for each mode when no measured leg exists.
_ASSUMED_SPEED_BY_MODE = {
    TravelMode.WALK: _WALKING_METERS_PER_MINUTE,
    TravelMode.TRANSIT: _TRANSIT_METERS_PER_MINUTE,
    TravelMode.TAXI: _DRIVING_METERS_PER_MINUTE,
    TravelMode.DRIVE: _DRIVING_METERS_PER_MINUTE,
}

#: How the assumption reads to a person, for the disclosure below.
#:
#: Written with Chinese units (每小时 N 公里) rather than ``km/h``. Both are understood by a
#: Chinese reader, but this prose is checked by ``test_user_facing_prose.py``, which flags runs
#: of Latin letters in text the user reads — and it flagged this on the first attempt. The check
#: was right and the wording was wrong: the rest of the sentence is Chinese, and a unit is not a
#: proper noun that has to keep its original spelling the way 高德 or OSRM do.
_ASSUMED_SPEED_TEXT = {
    TravelMode.WALK: "步行按每小时 4.5 公里估算",
    TravelMode.TRANSIT: "公交/地铁按每小时 15 公里估算，含候车与停站",
    TravelMode.TAXI: "打车按每小时 24 公里估算",
    TravelMode.DRIVE: "驾车按每小时 24 公里估算",
}


def assumed_speed_for(profile: TripProfile) -> tuple[int, str]:
    """The speed an unmeasured leg is estimated at, and how to say so.

    Returned as a pair because the number and its disclosure must not be able to disagree: the
    itinerary tells the traveller these minutes are 按距离估算, and a reader who wants to judge
    the number needs the speed behind it.

    This used to read only ``profile.pace``: RELAXED and BALANCED meant walking, FULL meant
    driving, and ``travel_mode`` was never consulted. That is a third derivation of the mode —
    ``resolve_travel_mode`` in ``providers/base.py`` already warns that the mode must be derived
    in one place, because the routing providers have to agree about it — and it produced two
    wrong answers in opposite directions, both reproducible on a ~13 km hop in 成都:

    ==================  ==============  ==========  =========
    request             old minutes     old speed   meaning
    ==================  ==============  ==========  =========
    FULL + walk         44              18.2 km/h   jogging
    RELAXED + drive     231             3.5 km/h    crawling
    ==================  ==============  ==========  =========

    FULL is an *itinerary density* — how many stops fit in a day — and it was silently choosing
    a car. A traveller who asked to walk and a traveller who asked to drive got the same number,
    and neither was told which assumption produced it.

    The mode is now resolved by the shared function, so a pace-derived mode keeps its old
    behaviour exactly (``resolve_travel_mode`` still maps RELAXED/BALANCED to walk and FULL to
    drive when ``travel_mode`` is unset) and an explicit mode is honoured.
    """
    mode = resolve_travel_mode(profile)
    speed = _ASSUMED_SPEED_BY_MODE.get(mode, _DRIVING_METERS_PER_MINUTE)
    return int(speed), _ASSUMED_SPEED_TEXT.get(mode, "按驾车速度估算")


def _derive_minutes(straight_line_meters: float, profile: TripProfile) -> int:
    road_meters = straight_line_meters * _ROAD_DETOUR_FACTOR
    speed, _ = assumed_speed_for(profile)
    return max(1, ceil(road_meters / speed))


def _pace_allowance(profile: TripProfile) -> int:
    """Last-resort allowance when nothing at all is known about a hop.

    Neither endpoint has coordinates, so there is nothing to measure and no distance to divide —
    this is the only figure here that cannot depend on the mode at all. It therefore stays a
    pace allowance, which is what it is named for and what it is: how long a gap this planner is
    willing to assume, scaled by how tightly the traveller wants the day packed. The mode is
    deliberately *not* consulted, because a mode with no distance has no speed to apply; claiming
    otherwise would be inventing a measurement out of a preference.
    """
    return max(5, ceil(15 * (1.4 if profile.pace is Pace.RELAXED else 1.0)))


def _leftover_reason(attraction: Attraction, days: list[ItineraryDay]) -> str:
    """Explain why an attraction could not be scheduled.

    Opening hours are only the cause when the attraction actually has a window on
    one of the trip's weekdays. Otherwise the real cause is that the itinerary ran
    out of slots, and blaming opening hours would send the user looking in the
    wrong place.
    """
    trip_weekdays = {day.day_date.weekday() for day in days}
    relevant = [window for window in attraction.opening_hours if window.weekday in trip_weekdays]
    if not relevant:
        return "not enough trip days to fit it in"
    return "opening hours do not match any available slot"
