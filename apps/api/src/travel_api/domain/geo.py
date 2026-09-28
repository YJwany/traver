"""Straight-line distance, in one place and named for what it is.

Two things here are deliberate.

**The name says "straight line".** This project's recurring failure is a derived number
being presented as a measured one: a car duration advertised as pedestrian, a distance-based
estimate labelled 实测, a default allowance shown as a quoted rate. A helper called
``distance`` invites exactly that; one called ``straight_line_meters`` cannot be mistaken for
a road distance by the next reader, and every caller that reports the result has to decide
how to say so.

**There is one implementation.** The itinerary optimizer and the place resolver both need
this, and two copies of a formula is how one of them quietly acquires a different earth
radius. The optimizer keeps its private name as an alias so its call sites stay readable.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

#: Mean earth radius in metres (IUGG). A sphere is accurate enough for "is this the same
#: city" and "is this place inside the destination area", which is all this is used for —
#: never for a stated travel time, which comes from a routing service or is disclosed as an
#: estimate.
EARTH_RADIUS_METERS = 6_371_000.0


def straight_line_meters(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    """Great-circle distance between two ``(latitude, longitude)`` pairs, in metres.

    Great-circle rather than planar: a planar approximation is fine over a city and badly
    wrong over the distances this is also used for, where rejecting a namesake 1,100 km away
    is the whole point.
    """
    latitude_1, longitude_1 = radians(origin[0]), radians(origin[1])
    latitude_2, longitude_2 = radians(destination[0]), radians(destination[1])
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = longitude_2 - longitude_1
    a = (
        sin(delta_latitude / 2) ** 2
        + cos(latitude_1) * cos(latitude_2) * sin(delta_longitude / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * asin(sqrt(a))


def straight_line_km(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    """The same distance in kilometres, rounded to one decimal.

    Rounded because the precision of a great-circle distance between two geocoded points
    is far below a metre — the coordinates themselves are the uncertainty. Printing metres
    would dress the number up as more measured than it is.
    """
    return round(straight_line_meters(origin, destination) / 1000.0, 1)
