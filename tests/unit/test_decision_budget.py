from datetime import date
from decimal import Decimal

import pytest
from travel_api.domain.models import BudgetItemStatus, TripProfile
from travel_api.providers.mock import MockTravelProvider, build_hotels
from travel_api.providers.registry import ProviderRegistry
from travel_api.services.aggregation import build_preview
from travel_api.services.budget_engine import calculate_budget
from travel_api.services.decision_engine import score_hotel_offers


@pytest.mark.asyncio
async def test_mock_aggregation_has_explainable_decisions_and_budget():
    profile = TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 7),
        travelers=2,
        total_budget=Decimal("6000"),
        hotel_budget=Decimal("1800"),
        preferences=["美食", "景点"],
        hotel_preferences=["地铁附近", "评分高"],
    )
    preview = await build_preview(profile, ProviderRegistry([MockTravelProvider("ctrip-mock")]))
    assert len(preview.hotels) == 2
    assert preview.hotel_decisions
    assert preview.hotel_decisions[0].score_breakdown.rating >= 0
    assert preview.budget.total > 0
    assert preview.budget.over_budget is False
    assert all(item.source_reference for item in preview.hotel_offers)


def test_budget_with_no_prices_falls_back_to_flagged_allowances():
    """A geo-only data layer has no prices; the budget must not silently be ¥0.

    Every fallback line has to be marked as an estimate so it can never be
    mistaken for an observed commercial fact.
    """
    profile = TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 7),
        travelers=2,
        total_budget=Decimal("6000"),
    )
    budget = calculate_budget(profile, None, [], [], [])
    by_category = {item.category: item for item in budget.items}

    assert budget.total > 0
    # The hotel line must exist even without a commercial offer.
    assert "hotel" in by_category
    assert by_category["hotel"].is_estimate is True
    assert by_category["hotel"].status == BudgetItemStatus.ESTIMATED

    for category in ("hotel", "food", "attraction", "transport"):
        assert by_category[category].amount > 0, f"{category} must not be silently zero"
        assert by_category[category].is_estimate is True
        assert by_category[category].status == BudgetItemStatus.ESTIMATED

    assert budget.over_budget is False


def test_observed_prices_are_not_replaced_by_fallbacks():
    """Real prices must win over the allowance, and not be flagged as estimates."""
    from travel_api.domain.models import Attraction, Money, PriceType

    profile = TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=1,
        total_budget=Decimal("6000"),
    )
    attraction = Attraction(
        canonical_name="武侯祠",
        location={
            "address": "武侯祠大街",
            "city": "成都",
            "coordinates": {"latitude": 30.6475, "longitude": 104.0432},
        },
        ticket_price=Money(value=Decimal("50"), currency="CNY", price_type=PriceType.PER_PERSON),
    )
    budget = calculate_budget(profile, None, [], [attraction], [])
    item = next(entry for entry in budget.items if entry.category == "attraction")
    assert item.amount == Decimal("50")
    assert item.is_estimate is False
    assert item.status == BudgetItemStatus.OBSERVED


def test_hard_hotel_budget_excludes_ineligible_offers():
    hotels, offers = build_hotels("ctrip-mock")
    profile = TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 7),
        travelers=2,
        hotel_budget=Decimal("100"),
    )
    assert score_hotel_offers(hotels, offers, profile) == []
