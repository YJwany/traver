from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from travel_api.domain.models import DataDomain, Money, PriceType, SourceReference, TripProfile


def test_trip_profile_rejects_non_positive_date_range():
    with pytest.raises(ValueError, match="end_date"):
        TripProfile(destination="成都", start_date=date(2026, 10, 7), end_date=date(2026, 10, 7))


def test_source_reference_reports_stale_data():
    reference = SourceReference(
        source="fixture",
        source_type="fixture",
        source_id="x",
        observed_at=datetime.now(timezone.utc),
        fetched_at=datetime.now(timezone.utc),
        expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        domain=DataDomain.COMMERCIAL_FACT,
    )
    assert reference.is_stale is True


def test_money_keeps_commercial_context():
    money = Money(value=Decimal("428"), currency="CNY", price_type=PriceType.PER_NIGHT)
    assert money.value == Decimal("428")
    assert money.price_type == PriceType.PER_NIGHT
