from decimal import Decimal

from travel_api.domain.models import (
    BudgetBreakdown,
    BudgetItem,
    BudgetItemStatus,
    BudgetStrategyName,
)
from travel_api.services.budget_engine import BudgetEngine


def test_budget_strategy_tracks_fact_status_and_reallocation():
    budget = BudgetBreakdown(
        items=[
            BudgetItem(category="hotel", amount=Decimal("3000"), status=BudgetItemStatus.OBSERVED),
            BudgetItem(category="food", amount=Decimal("1500")),
        ],
        total=Decimal("4500"),
        budget_limit=Decimal("4000"),
        remaining=Decimal("-500"),
        over_budget=True,
        strategy=BudgetStrategyName.STRICT,
    )
    options = BudgetEngine().reallocate(
        budget,
        alternative_hotel_savings=[Decimal("300")],
        food_savings=[Decimal("250")],
    )

    assert options
    assert sum(item.savings for item in options[0]) >= Decimal("500")
    assert budget.items[0].status is BudgetItemStatus.OBSERVED


def _profile():
    from travel_api.domain.models import TripProfile

    return TripProfile(
        destination="成都",
        start_date="2026-10-03",
        end_date="2026-10-05",
        travelers=2,
        total_budget=Decimal("5000"),
    )


def _quoted_offer():
    """A hotel rate that some source actually measured."""
    from datetime import date, datetime, timedelta, timezone
    from uuid import uuid4

    from travel_api.domain.models import (
        DataDomain,
        HotelOffer,
        Money,
        ObservationBasis,
        SourceReference,
        SourceType,
    )

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
            source="some-hotel-api",
            source_id="offer-1",
            source_type=SourceType.OFFICIAL_API,
            fetched_at=now,
            observed_at=now - timedelta(hours=1),
            observed_at_basis=ObservationBasis.PROVIDER_REPORTED,
            confidence=0.95,
            domain=DataDomain.COMMERCIAL_FACT,
        ),
    )


def _hotel_of(breakdown):
    return next(item for item in breakdown.items if item.category == "hotel")


def test_a_figure_with_no_quote_behind_it_is_flagged_as_an_estimate():
    """The fallback allowance must not be indistinguishable from a real rate.

    This is what makes the settings page's honest statement about room rates true: no
    integrated source can quote one, so the accommodation line is an allowance — and it says
    so. Without the flag, a number the engine invented would sit in the budget looking
    exactly like a rate a hotel gave us, which is the most expensive lie this tool could tell.
    """
    breakdown = BudgetEngine().calculate(_profile(), None, [], [], [])

    hotel = _hotel_of(breakdown)
    assert hotel.is_estimate is True
    assert hotel.status is BudgetItemStatus.ESTIMATED
    # Nothing was measured, so nothing may be cited.
    assert hotel.source_reference_ids == []
    # And the absence of prices must not collapse the traveller's largest cost to zero.
    assert hotel.amount > 0
    assert breakdown.total > 0


def test_a_quoted_rate_is_not_flagged_as_an_estimate():
    """The other direction, without which the flag carries no information.

    A label that is always on is not a label. Existing coverage for this lives in a
    live-network test that is skipped where the network is unavailable, so the invariant is
    pinned here, where it always runs.
    """
    breakdown = BudgetEngine().calculate(_profile(), _quoted_offer(), [], [], [])

    hotel = _hotel_of(breakdown)
    assert hotel.is_estimate is False
    assert hotel.status is not BudgetItemStatus.ESTIMATED
    # A quoted figure must be traceable to the source that quoted it.
    assert hotel.source_reference_ids
    assert hotel.amount == Decimal("1800")


def test_the_estimated_and_quoted_cases_are_actually_distinguishable():
    """Stated as the property that matters, so neither assertion can drift alone."""
    estimated = _hotel_of(BudgetEngine().calculate(_profile(), None, [], [], []))
    quoted = _hotel_of(BudgetEngine().calculate(_profile(), _quoted_offer(), [], [], []))

    assert estimated.is_estimate is not quoted.is_estimate
    assert estimated.status is not quoted.status
    # The estimated figure follows the traveller's budget rather than a source, so the two
    # are not even the same number — the distinction is visible in the amount as well.
    assert estimated.amount != quoted.amount
