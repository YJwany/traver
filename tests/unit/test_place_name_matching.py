"""Two sources, two spellings of the same place.

The defect: ``itinerary_fit`` scored ``0.9 if hotel.location.city == profile.destination else
0.2``. The traveller types 丽江; Nominatim reports ``address.city == "丽江市"`` while 高德 is
queried with the traveller's own text and echoes it back. So equality returned "a different
city" for every free-source hotel and "the same city" for every 高德 one — the identical hotel
scored 0.2 or 0.9 **depending on whether a key was configured**. Adding a key silently
reordered the recommendations.

The district field has the same problem for a different reason: Overpass fills it from OSM's
``suburb`` (「西湖街道」) and 高德 from ``businessarea`` (「西湖」), so the two describe the same
place at different granularities.

A comparison like this cannot be tested by asserting on one spelling. So the tests below
assert the *relation*: what should be equal, what should not, and what cannot be told.
"""

from __future__ import annotations

from datetime import date

import pytest
from travel_api.domain.models import Coordinates, Location, TripProfile
from travel_api.providers.mock import build_hotels
from travel_api.services.decision_engine import (
    OTHER_CITY_SCORE,
    SAME_CITY_SCORE,
    plan_geography,
    score_hotel_offers,
)
from travel_api.services.normalization import normalize_place_name, same_place_name


def test_the_suffix_a_traveller_omits_still_matches():
    """The exact case from the recorded response: 丽江 vs 丽江市."""
    assert same_place_name("丽江", "丽江市") is True
    assert same_place_name("丽江市", "丽江") is True


@pytest.mark.parametrize(
    ("left", "right", "why"),
    [
        ("杭州", "杭州市", "市 is the common omission"),
        ("西湖区", "西湖", "区 is the common omission"),
        ("西湖街道", "西湖", "OSM's suburb against 高德's businessarea"),
        ("西湖街道", "西湖区", "and across the two granularities at once"),
        ("北京市", "北京", "municipality"),
        ("新疆维吾尔自治区", "新疆", "自治区"),
        ("香港特别行政区", "香港", "SAR"),
    ],
)
def test_the_same_place_spelled_two_ways_matches(left, right, why):
    assert same_place_name(left, right) is True, why


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Different cities, one of which is a prefix of the other. Stripping the suffix must not
        # make these equal, which is the risk of normalising too aggressively.
        ("杭州", "杭州市余杭"),
        ("苏州", "徐州"),
        ("丽江", "丽江古城"),
    ],
)
def test_different_places_stay_different(left, right):
    assert same_place_name(left, right) is False


def test_an_absent_name_is_not_a_different_name():
    """Three-valued, deliberately. ``False`` would be a confident claim from no data."""
    assert same_place_name(None, "杭州") is None
    assert same_place_name("杭州", None) is None
    assert same_place_name("", "杭州") is None
    assert same_place_name("   ", "杭州") is None
    assert same_place_name(None, None) is None


def test_normalisation_does_not_return_an_empty_string_for_a_suffix_only_name():
    """``"市"`` is not a name. Empty would compare equal to another empty and look like a match."""
    assert normalize_place_name("市") is None
    assert normalize_place_name("区") is None
    assert normalize_place_name("  ") is None


def test_whitespace_and_full_width_forms_do_not_block_a_match():
    """NFKC and whitespace folding, so a stray space is not read as a different place."""
    assert same_place_name("杭 州", "杭州") is True
    assert same_place_name("杭州市 ", " 杭州") is True


def _profile(destination: str) -> TripProfile:
    return TripProfile(
        destination=destination,
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 7),
        travelers=2,
    )


def _hotels_named(city: str):
    hotels, offers = build_hotels("ctrip-mock")
    return (
        [
            hotel.model_copy(
                update={
                    "location": Location(
                        address="某路 1 号",
                        city=city,
                        district="古城区",
                        coordinates=Coordinates(latitude=26.87, longitude=100.23),
                    )
                }
            )
            for hotel in hotels
        ],
        offers,
    )


def test_a_hotel_in_the_destination_is_scored_as_local_however_the_city_is_spelled():
    """The defect, end to end: 高德's spelling and Nominatim's spelling must score the same.

    Both spellings denote the same city, so a hotel there is equally convenient. Before the
    fix one got 0.9 and the other 0.2 — a difference produced entirely by which provider
    answered, not by anything about the hotel.
    """
    profile = _profile("丽江")
    as_amap_spells_it, offers = _hotels_named("丽江")
    as_nominatim_spells_it, _ = _hotels_named("丽江市")

    scores = {}
    for label, hotels in (("高德", as_amap_spells_it), ("Nominatim", as_nominatim_spells_it)):
        decisions = score_hotel_offers(
            hotels, offers, profile, geography=plan_geography([h.location for h in hotels])
        )
        scores[label] = decisions[0].score_breakdown.itinerary_fit

    assert scores["高德"] == SAME_CITY_SCORE
    assert scores["Nominatim"] == SAME_CITY_SCORE
    assert scores["高德"] == scores["Nominatim"], (
        "the same city must score the same regardless of which provider named it"
    )


def test_a_hotel_in_another_city_is_still_penalised():
    """The guard against over-correcting: folding suffixes must not make everything local."""
    profile = _profile("丽江")
    hotels, offers = _hotels_named("丽江市")
    # 杭州 is not 丽江 under any spelling.
    elsewhere = hotels[1].model_copy(
        update={"location": hotels[1].location.model_copy(update={"city": "杭州市"})}
    )
    decisions = score_hotel_offers(
        [hotels[0], elsewhere], offers, profile, geography=plan_geography([hotels[0].location])
    )
    by_offer = {decision.candidate_id: decision for decision in decisions}
    assert by_offer[offers[0].id].score_breakdown.itinerary_fit == SAME_CITY_SCORE
    assert by_offer[offers[1].id].score_breakdown.itinerary_fit == OTHER_CITY_SCORE


def test_a_hotel_with_no_city_is_uncompared_rather_than_penalised():
    """An empty city is missing data, not evidence that the hotel is in another city."""
    profile = _profile("丽江")
    hotels, offers = _hotels_named("丽江市")
    unknown = hotels[1].model_copy(
        update={"location": hotels[1].location.model_copy(update={"city": ""})}
    )
    decisions = score_hotel_offers(
        [hotels[0], unknown], offers, profile, geography=plan_geography([hotels[0].location])
    )
    by_offer = {decision.candidate_id: decision for decision in decisions}
    assert by_offer[offers[1].id].score_breakdown.itinerary_fit is None
