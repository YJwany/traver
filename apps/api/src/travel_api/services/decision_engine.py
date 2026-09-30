"""Scoring a hotel offer against the plan it would be part of.

## The centre used to be a hardcoded 成都 coordinate

This module scored "how convenient is this hotel" by measuring the distance to the literal
point ``(30.6574, 104.0817)`` — 春熙路, 成都 — and by preferring hotels whose district was one
of ``{"市中心", "锦江区"}``. Those are the coordinates and the district names of the fixture
data in ``providers/mock.py``, which is what the code was first written against.

For a 成都 plan it looked right. For any other destination every hotel is hundreds or
thousands of kilometres from that point, so:

* ``transit`` scored **0.0 for every candidate** — not a low score, an identical one, so the
  dimension carried no information while still holding 20% of the weight;
* the explanation printed a measured-looking ``距中心行程点约 1600000 米``;
* ``location`` gave the same 0.6 to every hotel, since no 东京 district is called 锦江区.

Every existing test used ``destination="成都"``, so the fixture and the assertion shared the
assumption and nothing could see it.

## Where the centre comes from now

From the plan's own points. ``plan_geography`` takes the locations of the attractions and
restaurants that were just aggregated and derives two things: their centroid, and the district
most of them are in. That makes both quantities facts about *this* itinerary, and it is the
same reason ``route_optimizer`` builds its route from real coordinates rather than a city
centre it looked up separately.

## A dimension nobody could measure is absent, not zero

If the plan has no located points there is no centre, and neither ``transit`` nor ``location``
can be computed. The honest representation of that is ``None`` — see ``ScoreBreakdown`` — and
such a dimension is dropped from the weighted sum, which is then renormalised over the
dimensions that *were* measured.

The alternative, writing 0.0, is what the old code did to every non-成都 trip: a number that
reads as "worst possible transit" when the truth is "not measured". That is the same
absent-is-not-empty mistake this codebase keeps finding in other places, and it is worse here
than usual because a decision score is a number people act on.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from travel_api.domain.models import (
    CandidateDecision,
    Coordinates,
    Hotel,
    HotelOffer,
    Location,
    ScoreBreakdown,
    TripProfile,
)
from travel_api.services.entity_resolution import geo_distance_meters
from travel_api.services.normalization import same_place_name

#: Beyond this distance from the plan's centre a hotel earns no transit credit. A constant
#: rather than a tuning knob because it is a statement about walking in a city, not a
#: preference: 3 km is roughly the point where a hotel stops being walkable to everything.
TRANSIT_RADIUS_METERS = 3000.0

#: Scores for the coarse "is it in the district the plan is in" comparison. The two values
#: are the ones this module has always used; only the district they are compared against has
#: changed, from 成都's names to whatever district this plan actually sits in.
SAME_DISTRICT_SCORE = 0.9
OTHER_DISTRICT_SCORE = 0.6

#: Scores for "is it in the city the trip is about". Separate names from the district pair
#: above because they are separate judgements with separate failure modes — the city comes
#: from the provider's own field and needs suffix folding, the district from the plan's points.
SAME_CITY_SCORE = 0.9
OTHER_CITY_SCORE = 0.2


@dataclass(frozen=True)
class DecisionWeights:
    price: float = 0.25
    location: float = 0.20
    transit: float = 0.20
    rating: float = 0.10
    itinerary_fit: float = 0.15
    user_preference: float = 0.10

    def by_dimension(self) -> dict[str, float]:
        """The weights keyed by the ``ScoreBreakdown`` field they apply to.

        Keyed rather than positional so the scoring loop can iterate the dimensions it could
        actually measure; a positional sum could not skip one.
        """
        return {
            "price": self.price,
            "location": self.location,
            "transit": self.transit,
            "rating": self.rating,
            "itinerary_fit": self.itinerary_fit,
            "user_preference": self.user_preference,
        }


@dataclass(frozen=True)
class PlanGeography:
    """Where this plan actually is, derived from the plan's own points.

    ``center`` is ``None`` when no point had coordinates, and ``main_district`` is ``None``
    when either no point had a district or no district was shared. Both are ``None`` for a
    real reason — nothing was located — and callers must not substitute a default, because a
    substituted centre would silently reintroduce the hardcoded city this replaced.
    """

    center: Coordinates | None
    main_district: str | None


def plan_geography(locations: Sequence[Location]) -> PlanGeography:
    """Derive the centre and the main district from the plan's own points.

    The centroid is the arithmetic mean of the coordinates, which is what "中心行程点" should
    have meant all along. The main district is the modal one; ties are broken by taking the
    most common, and Counter's ordering makes an exact tie resolve deterministically to the
    value that appeared first, so two identical plans cannot score differently.
    """
    coordinates = [item.coordinates for item in locations if item.coordinates is not None]
    center = None
    if coordinates:
        center = Coordinates(
            latitude=sum(point.latitude for point in coordinates) / len(coordinates),
            longitude=sum(point.longitude for point in coordinates) / len(coordinates),
        )

    districts = [item.district for item in locations if item.district]
    main_district = Counter(districts).most_common(1)[0][0] if districts else None

    return PlanGeography(center=center, main_district=main_district)


def score_hotel_offers(
    hotels: list[Hotel],
    offers: list[HotelOffer],
    profile: TripProfile,
    weights: DecisionWeights | None = None,
    geography: PlanGeography | None = None,
) -> list[CandidateDecision]:
    """Rank the offers that satisfy the hard constraints.

    ``geography`` is what makes the transit and location dimensions meaningful. Omitting it is
    allowed and means "the caller has no plan geography", which drops those two dimensions
    rather than inventing a centre.
    """
    weights = weights or DecisionWeights()
    by_dimension = weights.by_dimension()
    hotel_by_id = {hotel.id: hotel for hotel in hotels}
    valid_offers = []
    for offer in offers:
        if offer.hotel_id not in hotel_by_id:
            continue
        if profile.hotel_budget is not None and offer.total_price > profile.hotel_budget:
            continue
        if offer.check_in != profile.start_date or offer.check_out != profile.end_date:
            continue
        if offer.availability is not None and offer.availability <= 0:
            continue
        if offer.source_reference.is_stale:
            continue
        valid_offers.append(offer)
    if not valid_offers:
        return []

    max_price = max(float(offer.total_price) for offer in valid_offers) or 1.0
    decisions: list[CandidateDecision] = []
    for offer in valid_offers:
        hotel = hotel_by_id[offer.hotel_id]

        # Distance to the plan's own centre, or None. The distance is kept so the explanation
        # can state the measurement it actually made; it is never inferred from a constant.
        distance_meters: float | None = None
        if geography is not None and geography.center is not None:
            distance_meters = geo_distance_meters(
                hotel.location.coordinates.latitude,
                hotel.location.coordinates.longitude,
                geography.center.latitude,
                geography.center.longitude,
            )

        # Whether the hotel is in the district the plan mostly sits in. Both sides must be
        # known: a hotel with no district is not "elsewhere", it is uncompared. The names are
        # folded for the same reason the city names are — the two providers describe a district
        # at different granularities (OSM's `suburb` is 「西湖街道」, 高德's `businessarea` is
        # 「西湖」), so comparing them literally asks whether two sources happened to spell it
        # identically rather than whether they meant the same place.
        same_district: bool | None = None
        if geography is not None:
            same_district = same_place_name(hotel.location.district, geography.main_district)

        measured: dict[str, float] = {
            "price": max(0.0, 1.0 - float(offer.total_price) / max_price),
            "user_preference": _preference_score(profile),
        }
        # The rating is *omitted* when no source published one, exactly like `transit` and
        # `location` below — not set to zero. `ScoreBreakdown.rating` documents `None` as "not
        # measured" and says in as many words that `0.0` is deliberately not used because it reads
        # as "worst possible", which is a measurement. This line wrote `(hotel.rating or 0) / 5`,
        # so a hotel whose rating nobody published was scored as if it had the worst one.
        #
        # It is not merely a wrong entry in the breakdown. The total is normalised over the
        # dimensions present, so the zero also sat in the denominator and shrank every score — a
        # silent penalty on the *common* case. Today every hotel in every deployment has
        # `rating=None`: OSM's `stars` is a self-declared classification rather than a guest score
        # and 高德 lists `rating` among its own `absent_fields`. That is also why no test caught
        # it — `providers/mock.py` returns 4.8 and 4.6, and every test in `test_hotel_scoring.py`
        # uses that fixture, which carries a field no shipped source produces.
        if hotel.rating is not None:
            measured["rating"] = hotel.rating / 5
        # `itinerary_fit` asks "is this hotel in the city the trip is about", and comparing the
        # two names by equality does not answer it. The traveller types 丽江; Nominatim reports
        # `address.city == "丽江市"`, while 高德 is queried with the traveller's own text and
        # echoes it back. So equality said "a different city" for every free-source hotel and
        # "the same city" for every 高德 one: the identical hotel scored 0.2 or 0.9 depending on
        # whether a key was configured, which means adding a key silently reordered the
        # recommendations. `same_place_name` compares what the names have in common and returns
        # None when either is absent, which is not the same as a different name.
        same_city = same_place_name(hotel.location.city, profile.destination)
        if same_city is not None:
            measured["itinerary_fit"] = SAME_CITY_SCORE if same_city else OTHER_CITY_SCORE
        transit = (
            None
            if distance_meters is None
            else max(0.0, 1.0 - min(distance_meters, TRANSIT_RADIUS_METERS) / TRANSIT_RADIUS_METERS)
        )
        location = (
            None
            if same_district is None
            else (SAME_DISTRICT_SCORE if same_district else OTHER_DISTRICT_SCORE)
        )
        if transit is not None:
            measured["transit"] = transit
        if location is not None:
            measured["location"] = location

        # Normalised over the dimensions that exist, so an unmeasurable one neither helps nor
        # hurts: without this, dropping a 0.20-weight dimension would silently rescale every
        # score toward zero and make cross-plan comparisons meaningless.
        total_weight = sum(by_dimension[name] for name in measured)
        score = sum(measured[name] * by_dimension[name] for name in measured) / total_weight

        # Built *from* `measured` rather than written out by hand, so the breakdown and the sum
        # cannot disagree. The hand-written version is how `itinerary_fit` came to be dropped
        # from the sum while the model still required it: a KeyError at best, and at worst a
        # breakdown reporting a dimension that was never part of the decision. A dimension that
        # was not measured is simply absent here, and the model reads that as `None`.
        breakdown = ScoreBreakdown(**measured)
        decisions.append(
            CandidateDecision(
                candidate_id=offer.id,
                score=round(score, 6),
                score_breakdown=breakdown,
                matched_constraints=_matched_constraints(measured),
                failed_constraints=[],
                explanations=_explanations(
                    offer=offer,
                    hotel=hotel,
                    distance_meters=distance_meters,
                    same_district=same_district,
                    same_city=same_city,
                    main_district=geography.main_district if geography else None,
                    destination=profile.destination,
                ),
            )
        )
    return sorted(decisions, key=lambda item: item.score, reverse=True)


def _preference_score(profile: TripProfile) -> float:
    """How well the hotel matches the traveller's stated preferences. An orthogonal axis.

    Unchanged in substance from the original: it looks for the words the traveller used. It
    stays a coarse 1.0/0.6 because the preference text is free-form and nothing here parses
    it; claiming more resolution than that would be inventing a measurement.
    """
    preference_text = " ".join(profile.hotel_preferences + profile.preferences).lower()
    return 1.0 if "地铁" in preference_text or "交通" in preference_text else 0.6


def _matched_constraints(measured: dict[str, float]) -> list[str]:
    """The dimensions that were actually satisfiable, named, derived from what was measured.

    Keyed off ``measured`` rather than off separately-passed flags, so the constraint list
    cannot claim a dimension the score did not use — which is the failure mode this whole
    module is recovering from.

    The unmeasurable ones are **left out rather than listed**. A constraint list that named
    "fresh offer" alongside "no centre to compare against" would read as though the second had
    been checked and passed.
    """
    matched = ["hotel budget", "date range", "availability", "fresh offer"]
    if "transit" in measured:
        matched.append("walkable to the plan's centre")
    if "location" in measured:
        matched.append("district compared against the plan")
    if "itinerary_fit" in measured:
        matched.append("same city as the trip")
    return matched


def _explanations(
    *,
    offer: HotelOffer,
    hotel: Hotel,
    distance_meters: float | None,
    same_district: bool | None,
    same_city: bool | None,
    main_district: str | None,
    destination: str,
) -> list[str]:
    """The sentences a reader sees for one offer.

    Everything here states a measurement that was made, and where none was possible it says
    so instead of printing a number. The old wording printed a distance derived from a
    hardcoded point, which is the one thing this must never do again.

    ``destination`` is passed in rather than read off the hotel for the one sentence that has to
    name it: the two are different values, and using the hotel's own city in the destination's
    slot is how that sentence came to name the wrong place.
    """
    lines = [
        f"总价 ¥{offer.total_price}，{offer.nights} 晚",
        f"评分 {hotel.rating or '暂无'}，评论数 {hotel.review_count or '暂无'}",
    ]
    if distance_meters is None:
        lines.append("行程里没有已定位的地点，无法给出到行程中心的距离")
    else:
        lines.append(f"距行程中心约 {round(distance_meters)} 米")

    if same_district is None:
        lines.append("酒店或行程缺少城区信息，未比较城区")
    else:
        # The district name is quoted rather than described, so the reader can check it.
        relation = "相同" if same_district else "不同"
        lines.append(f"所在城区与行程主要城区（{main_district}）{relation}")

    if same_city is None:
        # `same_place_name` returns None when *either* side is absent, and only the hotel's side
        # can be: `TripProfile.destination` is required and non-empty. So this branch means the
        # hotel did not say, and the destination is the thing we know.
        #
        # This used to print the destination slot as `hotel.location.city or '未知'` — the hotel's
        # own city, or 未知, sitting inside 「目的地（…）」. So a hotel with no city produced
        # 「无法与目的地（未知）比较」, telling the reader we could not compare against somewhere
        # they never named, while the place they *had* named went unmentioned. With a city
        # present it would have named the hotel's city as the destination, which is worse than
        # 未知: a wrong place stated confidently.
        lines.append(f"酒店没有上报所在城市，无法与目的地（{destination}）比较")
    elif not same_city:
        # Only said when it is a *finding*. Saying "different city" is a claim about the hotel,
        # and the reader deciding between offers should be told when one is somewhere else.
        lines.append(f"所在城市（{hotel.location.city}）与目的地不同")
    return lines
