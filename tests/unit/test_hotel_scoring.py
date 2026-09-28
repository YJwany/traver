"""Hotel scoring must measure *this* plan, not the city it was first written against.

The defect these tests reproduce: ``score_hotel_offers`` measured the distance from each hotel
to the literal point ``(30.6574, 104.0817)`` — 春熙路, 成都 — and preferred hotels whose district
was in ``{"市中心", "锦江区"}``. Both are properties of the fixture data in ``providers/mock.py``.

Every test in this file uses a destination that is **not** 成都, on purpose. The old tests all
used 成都, which is why the hardcoding was invisible for so long: the fixture and the assertion
shared the same assumption. A test written against the same city as the constant cannot fail.
"""

from __future__ import annotations

from datetime import date

import pytest
from travel_api.domain.models import Coordinates, Location, TripProfile
from travel_api.providers.mock import build_hotels
from travel_api.services.decision_engine import (
    OTHER_DISTRICT_SCORE,
    SAME_DISTRICT_SCORE,
    DecisionWeights,
    plan_geography,
    score_hotel_offers,
)

#: 西湖, 杭州. Roughly 1600 km from the 成都 coordinate that used to be hardcoded, so any number
#: the old code produced for this plan was wrong by three orders of magnitude.
HANGZHOU = Coordinates(latitude=30.2408, longitude=120.101)

#: Two districts of 杭州, neither of which is 市中心 or 锦江区.
XIHU = "西湖区"
SHANGCHENG = "上城区"


def _profile(destination: str = "杭州") -> TripProfile:
    return TripProfile(
        destination=destination,
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 7),
        travelers=2,
        preferences=["美食"],
        hotel_preferences=["安静"],
    )


def _hotels(*, first_district: str, second_district: str, city: str = "杭州"):
    """The fixture hotels, relocated to 杭州 and to named districts."""
    hotels, offers = build_hotels("ctrip-mock")
    relocated = [
        hotel.model_copy(
            update={
                "location": Location(
                    address=f"{district}某路 1 号",
                    city=city,
                    district=district,
                    coordinates=Coordinates(
                        # ~1 km apart, so ordering cannot be an accident of longitude.
                        latitude=HANGZHOU.latitude + offset * 0.009,
                        longitude=HANGZHOU.longitude,
                    ),
                )
            }
        )
        for hotel, district, offset in (
            (hotels[0], first_district, 0),
            (hotels[1], second_district, 1),
        )
    ]
    return relocated, offers


def test_a_plan_with_no_points_has_no_centre_rather_than_a_city():
    """``PlanGeography`` must not invent a centre. This is the whole defect in one assertion."""
    geography = plan_geography([])
    assert geography.center is None
    assert geography.main_district is None


def test_the_centre_is_the_mean_of_the_plans_own_points():
    """A real centroid, checkable by hand, so "the plan's centre" is not a vague promise."""
    places = [
        Location(address="a", city="杭州", coordinates=Coordinates(latitude=30.0, longitude=120.0)),
        Location(address="b", city="杭州", coordinates=Coordinates(latitude=32.0, longitude=122.0)),
    ]
    geography = plan_geography(places)
    assert geography.center is not None
    assert geography.center.latitude == pytest.approx(31.0)
    assert geography.center.longitude == pytest.approx(121.0)


def test_the_main_district_is_the_one_the_plan_is_mostly_in():
    places = [
        Location(
            address=f"p{index}",
            city="杭州",
            district=district,
            coordinates=Coordinates(latitude=30.0 + index / 100, longitude=120.0),
        )
        for index, district in enumerate([XIHU, XIHU, SHANGCHENG])
    ]
    assert plan_geography(places).main_district == XIHU


def test_a_non_chengdu_plan_gets_a_distance_to_its_own_centre():
    """The reproducer. Under the old code this distance was ~1,600,000 m for every hotel."""
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    geography = plan_geography([hotel.location for hotel in hotels])

    decisions = score_hotel_offers(hotels, offers, _profile(), geography=geography)
    assert decisions

    for decision in decisions:
        transit = decision.score_breakdown.transit
        assert transit is not None, "a located plan must produce a transit score"
        # The hotels are about 1 km from the centroid, so the credit is high. The old code gave
        # 0.0 to every one of them, identically — a dimension that carried no information while
        # still holding 20% of the weight.
        assert transit > 0.5, f"transit {transit} is too low for a hotel ~1 km from the centre"


def test_the_distance_in_the_explanation_is_the_one_that_was_measured():
    """Not merely present — in the right order of magnitude for this plan."""
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    geography = plan_geography([hotel.location for hotel in hotels])
    decisions = score_hotel_offers(hotels, offers, _profile(), geography=geography)

    metres = [
        int(line.split("约 ")[1].split(" 米")[0])
        for decision in decisions
        for line in decision.explanations
        if "距行程中心约" in line
    ]
    assert len(metres) == len(decisions), "every decision should state its own distance"
    for value in metres:
        # 1 km of separation, so < 20 km is generous and still nowhere near 成都.
        assert value < 20_000, f"a 杭州 hotel reported {value} m from the centre, which is not 杭州"


def test_the_district_is_compared_against_the_plans_district_not_a_hardcoded_one():
    """The second half of the hardcoding: ``{"市中心", "锦江区"}``.

    A plan in 西湖区 gives its 西湖区 hotel the same-district score. Under the old code both
    hotels scored 0.6, because neither district is a 成都 name.
    """
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    geography = plan_geography([hotel.location for hotel in hotels])
    assert geography.main_district == XIHU, "the fixture plan must be mostly in 西湖区"

    decisions = score_hotel_offers(hotels, offers, _profile(), geography=geography)
    by_hotel = {decision.candidate_id: decision for decision in decisions}
    scores = {
        hotel.id: by_hotel[offer.id].score_breakdown.location
        for hotel, offer in zip(hotels, offers, strict=True)
    }

    assert scores[hotels[0].id] == SAME_DISTRICT_SCORE, "a 西湖区 hotel in a 西湖区 plan is central"
    assert scores[hotels[1].id] == OTHER_DISTRICT_SCORE, "an 上城区 hotel is not"


def test_the_district_comparison_folds_the_two_spellings_the_providers_use():
    """Overpass and 高德 name the same district differently, so equality is the wrong question.

    Overpass fills a place's ``district`` from OSM's ``suburb`` (「西湖街道」); 高德 fills it
    from ``businessarea`` (「西湖」). Compared literally, a hotel in the district the trip is
    about scores as if it were in another one — and, as with the city, which answer you get
    depends on which provider was configured rather than on the hotel.

    This test was added because a mutation that restored raw equality for the district passed
    every other test in this file: they all used the *same* spelling on both sides, so folding
    made no observable difference. A comparison between two vocabularies has to be tested
    across the vocabularies.
    """
    # OSM's `suburb` spelling, against the plan's 高德 `businessarea` spelling below.
    overpass_spelling = "西湖街道"
    hotels, offers = _hotels(first_district=overpass_spelling, second_district=SHANGCHENG)

    # The plan's points are described the way 高德 describes them; the hotels the way OSM does.
    geography = plan_geography(
        [
            Location(
                address=f"p{index}",
                city="杭州",
                district=district,
                coordinates=Coordinates(latitude=30.24 + index / 100, longitude=120.10),
            )
            # 西湖区 twice and 上城区 once, so the plan is mostly in 西湖区.
            for index, district in enumerate((XIHU, XIHU, SHANGCHENG))
        ]
    )
    assert geography.main_district == XIHU

    decisions = score_hotel_offers(hotels, offers, _profile(), geography=geography)
    by_offer = {decision.candidate_id: decision for decision in decisions}
    assert by_offer[offers[0].id].score_breakdown.location == SAME_DISTRICT_SCORE, (
        f"{overpass_spelling} and {XIHU} are the same district; comparing them literally says otherwise"
    )
    assert by_offer[offers[1].id].score_breakdown.location == OTHER_DISTRICT_SCORE


def test_an_unmeasurable_dimension_is_absent_rather_than_zero():
    """A plan with no located points cannot have a transit or location score.

    ``0.0`` would read as "worst possible transit" when the truth is "not measured" — and a
    decision score is a number people act on, so the two must be distinguishable.
    """
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    decisions = score_hotel_offers(hotels, offers, _profile(), geography=plan_geography([]))
    assert decisions, "the offers are still valid; only the geography is missing"

    for decision in decisions:
        assert decision.score_breakdown.transit is None
        assert decision.score_breakdown.location is None
        assert decision.explanations
        assert not any("距行程中心约" in line for line in decision.explanations), (
            "an unmeasured distance must not be printed as a measurement"
        )
        assert any("无法给出到行程中心的距离" in line for line in decision.explanations)


def test_a_hotel_with_no_district_is_uncompared_rather_than_mislocated():
    """``None`` district is not "a different district". The distinction is the same one."""
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    stripped = [
        hotels[0],
        hotels[1].model_copy(
            update={
                "location": hotels[1].location.model_copy(update={"district": None}),
            }
        ),
    ]
    geography = plan_geography([hotel.location for hotel in hotels])
    decisions = score_hotel_offers(stripped, offers, _profile(), geography=geography)

    by_offer = {decision.candidate_id: decision for decision in decisions}
    known = by_offer[offers[0].id].score_breakdown.location
    unknown = by_offer[offers[1].id].score_breakdown.location
    assert known == SAME_DISTRICT_SCORE
    assert unknown is None, "a hotel with no district was compared against one anyway"


def test_the_score_is_renormalised_over_the_dimensions_that_could_be_measured():
    """Dropping a dimension must not silently shrink every score.

    Without renormalisation the two dropped dimensions (0.20 + 0.20 of 1.00) would vanish from
    the numerator and leave the denominator alone, so every score would be scaled toward zero
    and two plans would no longer be comparable. Computed by hand from the same weights.
    """
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    weights = DecisionWeights()
    decisions = score_hotel_offers(
        hotels, offers, _profile(), weights=weights, geography=plan_geography([])
    )
    assert decisions

    breakdown = decisions[0].score_breakdown
    expected = (
        breakdown.price * weights.price
        + breakdown.rating * weights.rating
        + breakdown.itinerary_fit * weights.itinerary_fit
        + breakdown.user_preference * weights.user_preference
    ) / (weights.price + weights.rating + weights.itinerary_fit + weights.user_preference)
    assert decisions[0].score == pytest.approx(expected, abs=1e-6)


def test_a_hotel_in_another_city_is_still_ranked_last():
    """The guard against over-correcting: itinerary fit is a separate axis and still applies.

    Relocating the centre must not make a hotel in a different city look convenient just
    because the plan has no points to compare it with.
    """
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    elsewhere = hotels[1].model_copy(
        update={
            "location": hotels[1].location.model_copy(
                update={
                    "city": "成都",
                    "coordinates": Coordinates(latitude=30.6574, longitude=104.0817),
                }
            )
        }
    )
    decisions = score_hotel_offers(
        [hotels[0], elsewhere], offers, _profile(), geography=plan_geography([])
    )
    by_offer = {decision.candidate_id: decision for decision in decisions}
    assert by_offer[offers[0].id].score > by_offer[offers[1].id].score
    assert by_offer[offers[1].id].score_breakdown.itinerary_fit == pytest.approx(0.2)


def test_a_stale_offer_is_still_excluded():
    """The freshness constraint was here before this change and must survive it."""
    from travel_api.domain.models import DataDomain
    from travel_api.providers.mock import source_reference

    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    stale = offers[0].model_copy(
        update={
            "source_reference": source_reference(
                "ctrip-mock", "stale-offer", DataDomain.COMMERCIAL_FACT, -100
            )
        }
    )
    decisions = score_hotel_offers(
        hotels,
        [stale, offers[1]],
        _profile(),
        geography=plan_geography([hotel.location for hotel in hotels]),
    )
    assert [decision.candidate_id for decision in decisions] == [offers[1].id]


def test_no_offers_means_no_decisions():
    """The empty case, which the budget path depends on."""
    hotels, _ = build_hotels("ctrip-mock")
    assert score_hotel_offers(hotels, [], _profile()) == []


def test_the_breakdown_reports_exactly_the_dimensions_that_were_scored():
    """The invariant that broke when the breakdown was written out by hand.

    The scorer normalises its weighted sum over the dimensions it could measure, then builds
    the breakdown. Those are two lists in two places, and originally the second was written out
    literally — which is how `itinerary_fit` came to be dropped from the sum while the model
    still required it. A ``KeyError`` is the lucky outcome; the unlucky one is a breakdown that
    reports a dimension the decision did not use, and a reader who then believes the ranking
    took it into account.

    So: recompute the score from the breakdown, using only the dimensions it actually contains,
    and require it to reproduce the stored score. If the breakdown claims a dimension the sum
    skipped (or omits one it used), the arithmetic will not match.
    """
    weights = DecisionWeights()
    by_dimension = weights.by_dimension()

    for geography in (plan_geography([]), None):
        hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
        decisions = score_hotel_offers(
            hotels,
            offers,
            _profile(),
            weights=weights,
            geography=(
                geography
                if geography is None
                else plan_geography([hotel.location for hotel in hotels])
            ),
        )
        assert decisions

        for decision in decisions:
            reported = {
                name: value
                for name, value in decision.score_breakdown.model_dump().items()
                if value is not None
            }
            total_weight = sum(by_dimension[name] for name in reported)
            recomputed = (
                sum(reported[name] * by_dimension[name] for name in reported) / total_weight
            )
            assert decision.score == pytest.approx(recomputed, abs=1e-6), (
                f"the breakdown {sorted(reported)} does not reproduce the stored score; "
                "a dimension was scored without being reported, or reported without being scored"
            )


def test_each_optional_dimension_is_absent_exactly_when_it_cannot_be_measured():
    """Per-dimension, so a failure names which one drifted.

    The aggregate check above would pass if a dimension were added to both lists together and
    wrongly populated, so this pins what ``None`` is supposed to mean for each field.
    """
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)
    located = plan_geography([hotel.location for hotel in hotels])

    with_geography = score_hotel_offers(hotels, offers, _profile(), geography=located)
    assert with_geography
    for decision in with_geography:
        assert decision.score_breakdown.transit is not None
        assert decision.score_breakdown.location is not None
        assert decision.score_breakdown.itinerary_fit is not None

    without = score_hotel_offers(hotels, offers, _profile(), geography=plan_geography([]))
    assert without
    for decision in without:
        # No located points: no centre, and no district to compare against.
        assert decision.score_breakdown.transit is None
        assert decision.score_breakdown.location is None
        # But the city is still on the hotel, so this one is unaffected by the missing plan
        # geography — which is exactly the distinction the aggregate check cannot see.
        assert decision.score_breakdown.itinerary_fit is not None

    no_city = [
        hotels[0],
        hotels[1].model_copy(
            update={"location": hotels[1].location.model_copy(update={"city": ""})}
        ),
    ]
    decisions = score_hotel_offers(no_city, offers, _profile(), geography=located)
    by_offer = {decision.candidate_id: decision for decision in decisions}
    assert by_offer[offers[0].id].score_breakdown.itinerary_fit is not None
    assert by_offer[offers[1].id].score_breakdown.itinerary_fit is None


def test_the_constraint_list_names_only_dimensions_that_were_scored():
    """`matched_constraints` must not claim a check that did not happen.

    A list saying "walkable to the plan's centre" beside an offer whose transit score is `None`
    reads as though proximity was verified. It is the same failure as the freshness panels:
    an absent check presented as a passed one.
    """
    hotels, offers = _hotels(first_district=XIHU, second_district=SHANGCHENG)

    located = score_hotel_offers(
        hotels, offers, _profile(), geography=plan_geography([h.location for h in hotels])
    )
    unlocated = score_hotel_offers(hotels, offers, _profile(), geography=plan_geography([]))

    for decision in located:
        assert any("centre" in item for item in decision.matched_constraints)
    for decision in unlocated:
        assert not any("centre" in item for item in decision.matched_constraints), (
            "the constraint list claims a proximity check that could not be made"
        )
        assert not any("district" in item for item in decision.matched_constraints)
