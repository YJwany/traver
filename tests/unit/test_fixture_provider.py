from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from travel_api.domain.models import TripProfile
from travel_api.providers.fixture import FixtureLoader, FixtureTravelProvider


@pytest.mark.asyncio
async def test_fixture_provider_maps_json_to_domain_models():
    root = Path(__file__).parents[1] / "fixtures"
    provider = FixtureTravelProvider("fixture-provider", root)
    profile = TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 7),
        travelers=2,
        total_budget=Decimal("5000"),
    )

    hotels = await provider.search_hotels(profile)
    offers = await provider.search_room_offers([str(hotels.data[0].id)], profile)
    restaurants = await provider.search_restaurants(profile)

    assert FixtureLoader(root).records("hotels")
    assert hotels.data[0].location.city == "成都"
    assert offers.data[0].check_in == profile.start_date
    assert restaurants.data[0].average_cost.value == Decimal("108")
