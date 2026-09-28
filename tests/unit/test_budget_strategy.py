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
