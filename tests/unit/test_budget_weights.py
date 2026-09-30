"""A budget strategy must change a number, or it is a label.

Five strategies are user-selectable — ``planning_jobs.py`` and ``routes.py`` accept
``strategy=strict|balanced|comfort|time_first|food_first`` — and
``docs/architecture/decision-engine.md`` said 「策略改变候选权重和替换顺序」.

They did not. ``BudgetStrategy`` carried four weights, ``calculate`` built the strategy object and
consumed exactly one field, ``name``, which it stored on the breakdown as a label. Every weight
was computed and dropped. Measured on one input before the fix:

    strict       3760  ['1600', '1600', '360', '200', '0']
    balanced     3760  ['1600', '1600', '360', '200', '0']
    comfort      3760  ['1600', '1600', '360', '200', '0']
    time_first   3760  ['1600', '1600', '360', '200', '0']
    food_first   3760  ['1600', '1600', '360', '200', '0']

The five names were five names for one behaviour. A traveller choosing 「省一点」 got the same
plan as one choosing 「吃好一点」, and nothing said so — the stored ``strategy`` field made the
choice look recorded.

These tests pin what a strategy may *and may not* move. The second half matters more: the easy way
to make this test stop failing is to multiply everything, which would turn a quoted rate into a
preference-weighted guess and misattribute it to the supplier.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from travel_api.domain.models import (
    Attraction,
    BudgetItemStatus,
    BudgetStrategyName,
    Coordinates,
    DataDomain,
    HotelOffer,
    Location,
    Money,
    ObservationBasis,
    Pace,
    Restaurant,
    RouteLeg,
    SourceReference,
    SourceType,
    TravelMode,
    TripProfile,
)
from travel_api.services.budget_engine import BudgetEngine, BudgetStrategy, PriceFallback

ALL_STRATEGIES = list(BudgetStrategyName)


def _location() -> Location:
    return Location(
        city="成都",
        address="某路 1 号",
        coordinates=Coordinates(latitude=30.65, longitude=104.05),
    )


def _profile() -> TripProfile:
    return TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        pace=Pace.BALANCED,
        travelers=2,
        total_budget=Decimal("5200"),
    )


def _priced_restaurants() -> list[Restaurant]:
    return [
        Restaurant(
            canonical_name="餐馆",
            location=_location(),
            average_cost=Money(value=Decimal("120"), currency="CNY"),
        )
    ]


def _priced_attractions() -> list[Attraction]:
    return [
        Attraction(
            canonical_name="景点",
            location=_location(),
            ticket_price=Money(value=Decimal("60"), currency="CNY"),
        )
    ]


def _quoted_offer() -> HotelOffer:
    now = datetime.now(timezone.utc)
    return HotelOffer(
        hotel_id=uuid4(),
        room_type_id=uuid4(),
        rate_plan_id=uuid4(),
        check_in=date(2026, 10, 3),
        check_out=date(2026, 10, 5),
        guests=2,
        price=Money(value=Decimal("1800"), currency="CNY"),
        source_reference=SourceReference(
            source="hotel-api",
            source_id="offer-1",
            source_type=SourceType.OFFICIAL_API,
            fetched_at=now,
            observed_at=now,
            observed_at_basis=ObservationBasis.PROVIDER_REPORTED,
            confidence=0.95,
            domain=DataDomain.COMMERCIAL_FACT,
        ),
    )


def _priced_legs() -> list[RouteLeg]:
    """A route leg with a fare, so the transport category is priced too.

    Needed because ``transport`` is the fourth allowance. Leaving it unpriced means the strategy
    still legitimately moves the total, so a "nothing moves when everything is priced" test would
    be asserting something false about its own fixture — which is what the first version of that
    test did: it priced three of four categories and then required all four to be inert.
    """
    now = datetime.now(timezone.utc)
    return [
        RouteLeg(
            origin_id=uuid4(),
            destination_id=uuid4(),
            mode=TravelMode.TAXI,
            distance_meters=4200,
            duration_minutes=14,
            cost=Money(value=Decimal("35"), currency="CNY", is_estimate=False),
            source_reference=SourceReference(
                source="some-router",
                source_id="leg-1",
                source_type=SourceType.OFFICIAL_API,
                fetched_at=now,
                observed_at=now,
                observed_at_basis=ObservationBasis.PROVIDER_REPORTED,
                confidence=0.9,
                domain=DataDomain.COMMERCIAL_FACT,
            ),
        )
    ]


def _amount(breakdown, category: str) -> Decimal:
    return next(item.amount for item in breakdown.items if item.category == category)


# -- the defect ----------------------------------------------------------------


def test_the_five_strategies_do_not_all_produce_the_same_plan():
    """The whole claim, in one assertion, with no reference to any weight.

    Before the fix this compared five identical totals. It is written as a count of distinct
    results rather than as five expected numbers so that it keeps working when the weights are
    retuned — what must not regress is that the choice *does* something.
    """
    profile = _profile()
    totals = {
        name: BudgetEngine()
        .calculate(profile, None, _priced_restaurants(), _priced_attractions(), [], name)
        .total
        for name in ALL_STRATEGIES
    }
    assert len(set(totals.values())) > 1, (
        f"every strategy produced the same total: { {k.value: str(v) for k, v in totals.items()} } "
        "— the strategy is being stored as a label rather than applied"
    )


def test_a_strategy_can_move_the_item_amounts_not_only_the_total():
    """A total that differs while every line is identical would mean arithmetic elsewhere.

    The line items are what the traveller reads, so the difference has to be visible there.
    """
    profile = _profile()
    amounts = {
        name: tuple(
            str(item.amount)
            for item in BudgetEngine()
            .calculate(profile, None, _priced_restaurants(), _priced_attractions(), [], name)
            .items
        )
        for name in ALL_STRATEGIES
    }
    assert len(set(amounts.values())) > 1, (
        f"every strategy produced identical line items: {next(iter(amounts.values()))}"
    )


# -- what a strategy may NOT move ----------------------------------------------


def test_a_quoted_rate_is_never_scaled_by_a_preference():
    """The line that must not be crossed.

    ``hotel_weight`` multiplies a hotel *allowance*. Applying it to ``selected_offer.total_price``
    would be multiplying a number a supplier measured by a preference, then presenting the result
    with that supplier's name and a fresh source reference attached — an invention wearing
    someone else's evidence. The easiest way to make the tests above pass is to do exactly that,
    so it is pinned separately.
    """
    profile = _profile()
    offer = _quoted_offer()
    quoted = {
        name: _amount(
            BudgetEngine().calculate(
                profile, offer, _priced_restaurants(), _priced_attractions(), [], name
            ),
            "hotel",
        )
        for name in ALL_STRATEGIES
    }
    assert len(set(quoted.values())) == 1, (
        f"a quoted hotel rate moved with the strategy: { {k.value: str(v) for k, v in quoted.items()} }"
    )
    assert next(iter(quoted.values())) == offer.total_price


def test_a_priced_category_keeps_its_observed_status_under_every_strategy():
    """The multipliers must not leak into the fact status either.

    An amount scaled by a preference is no longer an observation, so if scaling ever reached a
    priced category the status would have to change with it — and the test that would catch the
    scaling is this one.
    """
    profile = _profile()
    for name in ALL_STRATEGIES:
        breakdown = BudgetEngine().calculate(
            profile, _quoted_offer(), _priced_restaurants(), _priced_attractions(), [], name
        )
        for category in ("hotel", "food", "attraction"):
            item = next(item for item in breakdown.items if item.category == category)
            assert item.status is BudgetItemStatus.OBSERVED, (
                f"{category} is priced but {name.value} marked it {item.status.value}"
            )
            assert item.is_estimate is False, (
                f"{category} is priced but {name.value} flagged it as an estimate"
            )


def test_with_every_category_priced_the_strategy_changes_nothing():
    """Nothing is left to estimate, so there is nothing left for a preference to weight.

    This is the same fact as the two tests above, stated as the invariant it is: the strategy
    scales *allowances*, and with no allowances present it must be a complete no-op. All four
    categories have to be priced for that to be the claim being tested — see ``_priced_legs``.
    """
    profile = _profile()
    totals = {
        name: BudgetEngine()
        .calculate(
            profile,
            _quoted_offer(),
            _priced_restaurants(),
            _priced_attractions(),
            _priced_legs(),
            name,
        )
        .total
        for name in ALL_STRATEGIES
    }
    assert len(set(totals.values())) == 1, (
        f"a fully priced plan still moved with the strategy: "
        f"{ {k.value: str(v) for k, v in totals.items()} }. Every figure here came from a source; "
        "none of them is an allowance."
    )


# -- the multiplier itself -----------------------------------------------------


def test_an_unknown_category_is_weighted_neither_up_nor_down():
    """A category added later must not crash planning or be silently skewed.

    ``allowance_for`` has to answer for a name it has never seen. Raising would take the whole
    plan down over a bookkeeping gap; guessing would invent a preference. Neutral is the only
    defensible answer.
    """
    for name in ALL_STRATEGIES:
        assert BudgetStrategy.from_name(name).allowance_for("some-future-category") == Decimal("1")


def test_the_multiplier_is_exact_and_not_a_float_approximation():
    """`Decimal(1.4)` is 1.399999999999999911182158029987.

    Multiplying a money field by that puts binary noise into the total and into the item amount,
    which then shows up in an export as `2240.000000000000` — a number nobody can reconcile.
    Going through `str` keeps it exact.
    """
    strict = BudgetStrategy.from_name(BudgetStrategyName.STRICT)
    assert strict.allowance_for("hotel") == Decimal("1.4")
    assert str(strict.allowance_for("hotel")) == "1.4"

    breakdown = BudgetEngine().calculate(
        _profile(),
        None,
        _priced_restaurants(),
        _priced_attractions(),
        [],
        BudgetStrategyName.STRICT,
    )
    # 400/night * 2 nights * 2 travellers * 1.4 = 2240, exactly.
    assert _amount(breakdown, "hotel") == Decimal("2240.0")
    assert "000000" not in str(_amount(breakdown, "hotel"))


def test_balanced_is_the_identity():
    """The default strategy must reproduce the unweighted allowances exactly.

    ``balanced`` is what every caller passes unless it says otherwise, and before this change it
    was the only behaviour that existed. If it is not the identity, then the fix silently changed
    the budget for every plan that never asked for a strategy.
    """
    profile = _profile()
    fallback = PriceFallback()
    nights = profile.nights
    balanced = BudgetStrategy.from_name(BudgetStrategyName.BALANCED)
    assert balanced.allowance_for("hotel") == Decimal("1")
    assert balanced.allowance_for("food") == Decimal("1")
    assert balanced.allowance_for("transport") == Decimal("1")

    breakdown = BudgetEngine(fallback).calculate(
        profile, None, [], [], [], BudgetStrategyName.BALANCED
    )
    assert _amount(breakdown, "hotel") == fallback.hotel_per_night * nights * profile.travelers
    assert _amount(breakdown, "food") == (
        fallback.food_per_person_per_day * nights * profile.travelers
    )
    assert _amount(breakdown, "transport") == (
        fallback.transport_per_person_per_day * nights * profile.travelers
    )


def test_strict_estimates_higher_and_comfort_lower_for_a_hotel():
    """The names have to mean something a traveller would predict.

    Not the exact ordering of every pair — that is a tuning question — but the two ends: a
    「省一点」 allowance must not come out *lower* than a 「住好一点」 one, or the label is worse
    than absent.
    """
    profile = _profile()
    strict = _amount(
        BudgetEngine().calculate(profile, None, [], [], [], BudgetStrategyName.STRICT), "hotel"
    )
    comfort = _amount(
        BudgetEngine().calculate(profile, None, [], [], [], BudgetStrategyName.COMFORT), "hotel"
    )
    assert strict > comfort, (
        f"strict budgets a hotel at {strict} and comfort at {comfort}; the stricter strategy must "
        "not assume a larger allowance than the more comfortable one"
    )


def test_food_first_spends_more_on_food_than_time_first():
    """The other axis, so a single global multiplier cannot satisfy both tests.

    If every strategy scaled every category by one number, the two orderings here and above could
    not both hold unless the strategies coincidentally ranked the same way — which would make
    `food_first` and `comfort` the same strategy under two names.
    """
    profile = _profile()
    food_first = _amount(
        BudgetEngine().calculate(profile, None, [], [], [], BudgetStrategyName.FOOD_FIRST), "food"
    )
    time_first = _amount(
        BudgetEngine().calculate(profile, None, [], [], [], BudgetStrategyName.TIME_FIRST), "food"
    )
    assert food_first > time_first

    # And time_first is the one that spends more on getting around.
    food_first_transport = _amount(
        BudgetEngine().calculate(profile, None, [], [], [], BudgetStrategyName.FOOD_FIRST),
        "transport",
    )
    time_first_transport = _amount(
        BudgetEngine().calculate(profile, None, [], [], [], BudgetStrategyName.TIME_FIRST),
        "transport",
    )
    assert time_first_transport > food_first_transport


@pytest.mark.parametrize("name", ALL_STRATEGIES)
def test_an_allowance_is_always_flagged_as_an_estimate(name):
    """Scaling an allowance keeps it an allowance.

    A weighted guess is still a guess; if a multiplier could promote it to ``OBSERVED`` the budget
    would present a preference as a price.
    """
    breakdown = BudgetEngine().calculate(_profile(), None, [], [], [], name)
    for item in breakdown.items:
        if item.amount > 0:
            assert item.is_estimate is True, f"{item.category} lost its estimate flag under {name}"
            assert item.status is BudgetItemStatus.ESTIMATED
