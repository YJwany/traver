"""Does the budget-weight suite actually pin strategy behaviour?

The tests were written after the fix, so they could pass for an unrelated reason. This restores the
original defect — the weights computed and then read nowhere — and requires the suite to fail.

The mutations go in both directions, which matters more here than in the other harnesses: the
cheapest way to make "the strategies differ" pass is to scale everything, including figures a
supplier quoted. That would trade one honesty defect for a worse one, so the cases that protect a
quoted rate are covered too.
"""

from __future__ import annotations

from _mutation_harness import ROOT, Case, run_harness

ENGINE = ROOT / "apps/api/src/travel_api/services/budget_engine.py"

CASES = [
    Case(
        "M1 the original defect: the strategy is stored as a label and applied to nothing",
        ENGINE,
        '            amount=self._allowance("hotel", selected_strategy, profile, nights),',
        "            amount=self.fallback.hotel_per_night * nights * profile.travelers,",
    ),
    Case(
        "M2 only the hotel allowance is weighted, so the other two stay inert",
        ENGINE,
        '            food = self._allowance("food", selected_strategy, profile, nights)',
        "            food = self.fallback.food_per_person_per_day * nights * profile.travelers",
    ),
    Case(
        "M3 the transport allowance stays inert",
        ENGINE,
        '            transport = self._allowance("transport", selected_strategy, profile, nights)',
        "            transport = (\n"
        "                self.fallback.transport_per_person_per_day * nights * profile.travelers\n"
        "            )",
    ),
    Case(
        "M4 every category gets one global multiplier, so food_first == time_first",
        ENGINE,
        """        weight = {
            "hotel": self.hotel_weight,
            "food": self.food_weight,
            "transport": self.transport_weight,
        }.get(category)""",
        """        weight = self.hotel_weight if category in ("hotel", "food", "transport") else None""",
    ),
    Case(
        "M5 an unknown category is guessed at a non-neutral weight instead of 1",
        ENGINE,
        '        if weight is None:\n            return Decimal("1")',
        '        if weight is None:\n            return Decimal("1.5")',
    ),
    Case(
        "M6 the multiplier goes through a float and puts binary noise in the money field",
        ENGINE,
        "        return Decimal(str(weight))",
        "        return Decimal(weight)",
    ),
    Case(
        "M7 balanced stops being the identity, changing every default plan",
        ENGINE,
        "            BudgetStrategyName.BALANCED: cls(name, 1.0, 1.0, 1.0, 1.0),",
        "            BudgetStrategyName.BALANCED: cls(name, 1.1, 1.1, 1.1, 1.0),",
    ),
    Case(
        "M8 strict guesses a LOWER hotel allowance than comfort",
        ENGINE,
        "            BudgetStrategyName.STRICT: cls(name, 1.4, 0.9, 1.0, 1.3),",
        "            BudgetStrategyName.STRICT: cls(name, 0.5, 0.9, 1.0, 1.3),",
    ),
    Case(
        "M9 a quoted hotel rate is scaled by a preference instead of left alone",
        ENGINE,
        "                    amount=selected_offer.total_price,",
        "                    amount=selected_offer.total_price"
        ' * selected_strategy.allowance_for("hotel"),',
    ),
]

if __name__ == "__main__":
    raise SystemExit(
        run_harness(
            files=(ENGINE,),
            cases=CASES,
            test_targets="tests/unit/test_budget_weights.py tests/unit/test_budget_strategy.py",
        )
    )
