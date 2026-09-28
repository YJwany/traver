from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FreshnessPolicy:
    hotel_offer: int = 300
    hotel_availability: int = 120
    restaurant: int = 1800
    attraction: int = 3600
    weather: int = 900
    route: int = 600

    def ttl_for(self, data_type: str, provider: str | None = None) -> int:
        del provider
        return int(getattr(self, data_type, self.attraction))


class FreshnessService:
    def __init__(self, policy: FreshnessPolicy | None = None) -> None:
        self.policy = policy or FreshnessPolicy()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def status(
        self,
        expires_at: datetime | None,
        *,
        observed_at: datetime | None = None,
        data_type: str | None = None,
        now: datetime | None = None,
    ) -> FreshnessStatus:
        current = now or datetime.now(timezone.utc)
        if expires_at is None and observed_at is None and data_type is None:
            return FreshnessStatus.UNKNOWN
        if expires_at is None and observed_at is not None and data_type is not None:
            expires_at = self._aware(observed_at) + timedelta(
                seconds=self.policy.ttl_for(data_type)
            )
        if expires_at is None:
            return FreshnessStatus.UNKNOWN
        return FreshnessStatus.FRESH if self._aware(expires_at) > current else FreshnessStatus.STALE

    def is_fresh(self, expires_at: datetime | None, **kwargs) -> bool:
        return self.status(expires_at, **kwargs) is FreshnessStatus.FRESH

    def is_stale(self, expires_at: datetime | None, **kwargs) -> bool:
        return self.status(expires_at, **kwargs) is FreshnessStatus.STALE

    def expires_in(
        self, expires_at: datetime | None, now: datetime | None = None
    ) -> timedelta | None:
        if expires_at is None:
            return None
        return self._aware(expires_at) - (now or datetime.now(timezone.utc))

    def remaining_ttl(self, expires_at: datetime | None, now: datetime | None = None) -> int | None:
        remaining = self.expires_in(expires_at, now)
        if remaining is None:
            return None
        return max(0, int(remaining.total_seconds()))
