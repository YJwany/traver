from datetime import datetime, timedelta, timezone

from travel_api.services.freshness import FreshnessPolicy, FreshnessService, FreshnessStatus


def test_freshness_service_uses_expiry_and_ttl_policy():
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    service = FreshnessService(FreshnessPolicy(hotel_offer=300))
    observed = now - timedelta(seconds=100)

    assert (
        service.status(None, observed_at=observed, data_type="hotel_offer", now=now)
        is FreshnessStatus.FRESH
    )
    assert service.is_stale(now - timedelta(seconds=1), now=now)
    assert service.remaining_ttl(now + timedelta(seconds=25), now=now) == 25
