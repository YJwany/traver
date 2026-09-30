from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from travel_api.domain.models import (
    Attraction,
    BudgetBreakdown,
    BudgetItem,
    BudgetItemStatus,
    BudgetStrategyName,
    HotelOffer,
    Restaurant,
    RouteLeg,
    TripProfile,
)


@dataclass(frozen=True)
class PriceFallback:
    """Documented allowances used when no priced source exists for a category.

    The free data layer (OpenStreetMap, Open-Meteo, OSRM) carries geo facts but
    no commercial prices. Without a fallback the whole budget collapses to zero,
    which is arguably worse than an explicit estimate: it silently hides the
    traveller's largest cost. These values are planning allowances, never facts,
    and every item they produce is flagged ``is_estimate=True``.

    Override them from settings when a real commercial provider is added.
    """

    hotel_per_night: Decimal = Decimal("400")
    food_per_person_per_day: Decimal = Decimal("120")
    attraction_per_person_per_day: Decimal = Decimal("80")
    transport_per_person_per_day: Decimal = Decimal("50")


@dataclass(frozen=True)
class BudgetAdjustment:
    category: str
    description: str
    savings: Decimal
    impact_score: float
    source_id: str | None = None


@dataclass(frozen=True)
class BudgetStrategy:
    """How a traveller's stated priority shifts the *allowances* this engine makes.

    The weights used to be computed here and read nowhere: ``calculate`` called
    ``BudgetStrategy.from_name`` and consumed exactly one field, ``name``, which it stored on the
    breakdown as a label. Measured before the fix, all five strategies produced the same total and
    the same item amounts on the same input — strict, balanced, comfort, time_first and food_first
    were five names for one behaviour, while the strategy is user-selectable (``planning_jobs.py``,
    ``routes.py``) and ``docs/architecture/decision-engine.md`` said in as many words that
    「策略改变候选权重和替换顺序」. It did not.

    What a strategy may honestly change is narrower than that sentence implied, and the narrowing
    is the point:

    * **It scales allowances, never prices.** ``hotel_weight`` cannot move a quoted rate — that is
      a fact one source measured, and multiplying it by a preference would turn an observation
      into an invention with a supplier's name attached. It only shifts the fallback figure used
      when *no* source priced the category, where the number was already an assumption.
    * **It therefore does nothing at all once every category is priced**, which is exactly right:
      there is nothing left to estimate, so there is nothing left for a preference to weight.
    * **It does not reorder anything.** The field names invite the reading that they rank
      candidates; they do not, and the docstring says so rather than letting the name imply it —
      hotel and route *ranking* happens in ``decision_engine``, under ``DecisionWeights``.

    So ``comfort`` does not make a hotel cheaper; it makes the engine's *guess* about an unpriced
    hotel more generous.

    There are three weights and there are **four** fallback allowances: ``attraction`` is one too
    (see ``BudgetEngine.calculate``), and no strategy scales it. That is a real gap and not an
    oversight being excused — ``allowance_for`` answers ``1`` for it, so it behaves exactly like a
    category this class has never heard of. Adding an ``attraction_weight`` means deciding a
    direction for each of the five strategies, and a wrong direction there is a wrong *estimate*
    rather than a wrong fact, so leaving it neutral is the honest default until that decision is
    actually made. A reader comparing this list against the code will find the fourth allowance,
    which is why it is named here instead of being left to be discovered.
    """

    name: BudgetStrategyName
    hotel_weight: float
    food_weight: float
    transport_weight: float
    comfort_penalty: float

    @classmethod
    def from_name(cls, name: BudgetStrategyName | str) -> BudgetStrategy:
        name = BudgetStrategyName(name)
        configurations = {
            BudgetStrategyName.STRICT: cls(name, 1.4, 0.9, 1.0, 1.3),
            BudgetStrategyName.BALANCED: cls(name, 1.0, 1.0, 1.0, 1.0),
            BudgetStrategyName.COMFORT: cls(name, 0.7, 1.2, 0.8, 0.6),
            BudgetStrategyName.TIME_FIRST: cls(name, 0.6, 0.8, 1.5, 0.5),
            BudgetStrategyName.FOOD_FIRST: cls(name, 0.8, 1.6, 0.9, 0.8),
        }
        return configurations[name]

    def allowance_for(self, category: str) -> Decimal:
        """The multiplier this strategy applies to one category's fallback allowance.

        Returns ``1`` for an unknown category rather than raising: a new ``BudgetItem`` category
        added later must not be able to crash planning, and an unweighted allowance is the
        neutral default rather than a guess.

        ``comfort_penalty`` is deliberately *not* a fourth multiplier. It is the one weight whose
        meaning is "this strategy dislikes giving something up", which is about reallocation
        rather than about a category, and applying it to a category total would make ``comfort``
        the most expensive strategy in every category at once — the opposite of what its name
        promises.
        """
        weight = {
            "hotel": self.hotel_weight,
            "food": self.food_weight,
            "transport": self.transport_weight,
        }.get(category)
        if weight is None:
            return Decimal("1")
        # Through `str` because a float cannot be a Decimal exactly; `Decimal(1.4)` is
        # 1.399999999999999911182158029987 and would put that noise into a money field.
        return Decimal(str(weight))


class BudgetEngine:
    def __init__(self, fallback: PriceFallback | None = None) -> None:
        self.fallback = fallback or PriceFallback()

    def _allowance(
        self,
        category: str,
        strategy: BudgetStrategy,
        profile: TripProfile,
        nights: int,
    ) -> Decimal:
        """The per-category figure used when no source priced it, shaped by the strategy.

        One place, so the three categories cannot drift in how they apply the multiplier — and so
        it is obvious that this is the *only* thing a strategy touches.
        """
        base = {
            "hotel": self.fallback.hotel_per_night,
            "food": self.fallback.food_per_person_per_day,
            "transport": self.fallback.transport_per_person_per_day,
        }[category]
        return base * nights * profile.travelers * strategy.allowance_for(category)

    def calculate(
        self,
        profile: TripProfile,
        selected_offer: HotelOffer | None,
        restaurants: list[Restaurant],
        attractions: list[Attraction],
        routes: list[RouteLeg],
        strategy: BudgetStrategyName | str = BudgetStrategyName.BALANCED,
    ) -> BudgetBreakdown:
        selected_strategy = BudgetStrategy.from_name(strategy)
        nights = max(profile.nights, 1)
        items: list[BudgetItem] = []

        # -- hotel ---------------------------------------------------------
        if selected_offer:
            items.append(
                BudgetItem(
                    category="hotel",
                    amount=selected_offer.total_price,
                    status=BudgetItemStatus.OBSERVED,
                    source_reference_ids=[selected_offer.source_reference.source_id],
                    is_estimate=False,
                )
            )
        else:
            # No commercial offer exists (for example an OSM-only data layer).
            # The hotel line must still appear, clearly marked as an allowance.
            items.append(
                BudgetItem(
                    category="hotel",
                    amount=self._allowance("hotel", selected_strategy, profile, nights),
                    status=BudgetItemStatus.ESTIMATED,
                    is_estimate=True,
                )
            )

        # -- food ----------------------------------------------------------
        priced_restaurants = [
            restaurant for restaurant in restaurants if restaurant.average_cost is not None
        ]
        if priced_restaurants:
            food_per_person = sum(
                (restaurant.average_cost.value for restaurant in priced_restaurants),
                Decimal("0"),
            )
            food = food_per_person * profile.travelers * nights
            food_estimated = all(
                restaurant.average_cost.is_estimate for restaurant in priced_restaurants
            )
        else:
            food = self._allowance("food", selected_strategy, profile, nights)
            food_estimated = True
        items.append(
            BudgetItem(
                category="food",
                amount=food,
                status=(
                    BudgetItemStatus.ESTIMATED if food_estimated else BudgetItemStatus.OBSERVED
                ),
                is_estimate=food_estimated,
            )
        )

        # -- attractions ---------------------------------------------------
        priced_attractions = [
            attraction for attraction in attractions if attraction.ticket_price is not None
        ]
        if priced_attractions:
            attraction = (
                sum((item.ticket_price.value for item in priced_attractions), Decimal("0"))
                * profile.travelers
            )
            attraction_estimated = all(item.ticket_price.is_estimate for item in priced_attractions)
            attraction_refs = [
                ref.source_id for item in priced_attractions for ref in item.source_references
            ]
        else:
            attraction = self.fallback.attraction_per_person_per_day * profile.travelers * nights
            attraction_estimated = True
            attraction_refs = []
        items.append(
            BudgetItem(
                category="attraction",
                amount=attraction,
                status=(
                    BudgetItemStatus.ESTIMATED
                    if attraction_estimated
                    else BudgetItemStatus.OBSERVED
                ),
                source_reference_ids=attraction_refs,
                is_estimate=attraction_estimated,
            )
        )

        # -- transport -----------------------------------------------------
        priced_routes = [route for route in routes if route.cost is not None]
        if priced_routes:
            transport = (
                sum((route.cost.value for route in priced_routes), Decimal("0")) * profile.travelers
            )
            transport_estimated = all(route.cost.is_estimate for route in priced_routes)
            transport_refs = [route.source_reference.source_id for route in priced_routes]
        else:
            # Walking legs carry no fare, so a car-free plan has no observed
            # transport cost at all.
            transport = self._allowance("transport", selected_strategy, profile, nights)
            transport_estimated = True
            transport_refs = []
        items.append(
            BudgetItem(
                category="transport",
                amount=transport,
                status=(
                    BudgetItemStatus.ESTIMATED if transport_estimated else BudgetItemStatus.OBSERVED
                ),
                source_reference_ids=transport_refs,
                is_estimate=transport_estimated,
            )
        )

        items.append(
            BudgetItem(
                category="miscellaneous",
                amount=Decimal("0"),
                status=BudgetItemStatus.PLANNED,
                is_estimate=True,
            )
        )
        total = sum((item.amount for item in items), Decimal("0"))
        remaining = profile.total_budget - total if profile.total_budget is not None else None
        return BudgetBreakdown(
            items=items,
            total=total,
            budget_limit=profile.total_budget,
            remaining=remaining,
            over_budget=remaining is not None and remaining < 0,
            strategy=selected_strategy.name,
        )

    def reallocate(
        self,
        budget: BudgetBreakdown,
        alternative_hotel_savings: list[Decimal] = (),
        food_savings: list[Decimal] = (),
        transport_savings: list[Decimal] = (),
    ) -> list[list[BudgetAdjustment]]:
        if not budget.over_budget or budget.remaining is None:
            return []
        target = abs(budget.remaining)
        options: list[list[BudgetAdjustment]] = []
        for savings in alternative_hotel_savings:
            if savings >= target:
                options.append(
                    [
                        BudgetAdjustment(
                            category="hotel",
                            description="replace hotel with a lower-cost eligible offer",
                            savings=savings,
                            impact_score=0.8,
                        )
                    ]
                )
        for savings in food_savings:
            if savings >= target:
                options.append(
                    [
                        BudgetAdjustment(
                            category="food",
                            description="replace one meal with a lower-cost candidate",
                            savings=savings,
                            impact_score=0.6,
                        )
                    ]
                )
        for hotel_saving in alternative_hotel_savings:
            for food_saving in food_savings:
                if hotel_saving + food_saving >= target:
                    options.append(
                        [
                            BudgetAdjustment(
                                category="hotel",
                                description="replace hotel with a lower-cost eligible offer",
                                savings=hotel_saving,
                                impact_score=0.8,
                            ),
                            BudgetAdjustment(
                                category="food",
                                description="replace one meal with a lower-cost candidate",
                                savings=food_saving,
                                impact_score=0.6,
                            ),
                        ]
                    )
        for savings in transport_savings:
            if savings >= target:
                options.append(
                    [
                        BudgetAdjustment(
                            category="transport",
                            description="use a lower-cost route",
                            savings=savings,
                            impact_score=0.7,
                        )
                    ]
                )
        return sorted(
            options,
            key=lambda option: (
                sum(item.impact_score for item in option),
                -sum(item.savings for item in option),
                len(option),
            ),
        )


def calculate_budget(
    profile: TripProfile,
    selected_offer: HotelOffer | None,
    restaurants: list[Restaurant],
    attractions: list[Attraction],
    routes: list[RouteLeg],
) -> BudgetBreakdown:
    return BudgetEngine().calculate(profile, selected_offer, restaurants, attractions, routes)
