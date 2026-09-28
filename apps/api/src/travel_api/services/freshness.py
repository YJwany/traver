from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from travel_api.domain.models import SourceReference


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


#: How old a fact may be *in its own terms* before it must not be presented as
#: current, whatever our cache says.
#:
#: These are deliberately different from the cache TTLs above. A TTL answers "when
#: should we ask again"; this answers "when has this stopped being true". A trip is
#: planned for a future date, so a fact that is too old to rely on is worse than a
#: missing one: it looks like knowledge.
#:
#: A day for a restaurant or a hotel listing is generous — openings and closures are
#: not announced to us — while an hour for a route reflects how quickly road works and
#: service changes invalidate it. Weather is the tightest because a superseded
#: forecast is actively misleading: it will describe yesterday's rain with the same
#: confidence as tomorrow's.
MAX_AGE_SECONDS: dict[str, int] = {
    "weather": 3600,
    "route": 3600,
    # Availability is the fact most likely to change under a traveller's feet.
    "hotel_availability": 1800,
    "hotel_offer": 43200,
    "restaurant": 86400,
    "hotel": 86400,
    "attraction": 86400,
    # Deliberately absent: ``user_content``.
    #
    # A note the traveller supplied is judged by *its own* stated publication date, and
    # that date can be years old without the note being unusable — a well-written
    # neighbourhood guide does not expire on a schedule the way a forecast does. What
    # matters is that the age is *disclosed*, not that some threshold is enforced. Giving
    # it a horizon here would produce a warning on every genuinely useful old guide,
    # which is how a reader learns to ignore warnings. Undated notes are still reported
    # as age-unknown through the normal ``SOURCE_SILENT`` path.
}


@dataclass(frozen=True)
class FreshnessPolicy:
    hotel_offer: int = 300
    hotel_availability: int = 120
    restaurant: int = 1800
    attraction: int = 3600
    weather: int = 900
    route: int = 600
    #: Overrides for :data:`MAX_AGE_SECONDS`, so an operator can loosen or tighten the
    #: horizon without editing code.
    max_age: dict[str, int] | None = None

    def ttl_for(self, data_type: str, provider: str | None = None) -> int:
        del provider
        return int(getattr(self, data_type, self.attraction))

    def max_age_for(self, data_type: str) -> int | None:
        """The age horizon for ``data_type``, or ``None`` when it has none.

        ``None`` means "we do not claim to know how fast this changes", and callers
        must treat that as "no staleness claim" rather than as "infinitely fresh".
        """
        if self.max_age and data_type in self.max_age:
            return int(self.max_age[data_type])
        return MAX_AGE_SECONDS.get(data_type)


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

    # -- data age, as opposed to cache validity -----------------------------

    def age_is_known(self, reference: SourceReference) -> bool:
        """Whether the reference tells us how old the *data* is.

        Only a provider-reported observation time does. A live-computed route is
        genuinely fresh but tells us nothing about an underlying dataset, and a silent
        source tells us nothing at all — so neither may be counted as evidence that a
        fact is recent.
        """
        return reference.observed_at_basis.age_is_about_the_data

    def audit(
        self,
        references: list[tuple[str, SourceReference]],
        *,
        now: datetime | None = None,
    ) -> list[dict]:
        """Facts too old to present as current, with the reason stated.

        ``references`` is ``(data_type, reference)`` pairs, because the acceptable age
        depends on what the fact *is*: a two-hour-old forecast is stale, a two-hour-old
        restaurant listing is not. The caller knows the data type; a reference does not.

        Three distinct outcomes, kept separate because the remedies differ:

        * **too old** — the source stamped the data and that stamp is past the horizon.
          Re-fetch, or present it as historical.
        * **age unknown** — the source is silent, so we cannot vouch for recency. Not an
          error, but not evidence of freshness either, and a plan must not imply
          otherwise by omission.
        * **no horizon** — nothing is claimed about this kind of fact, so it is not
          reported here at all rather than reported as fine.

        The first case is what the "recent data only" rule is about, and it is the one
        a silently-cached implementation would hide.
        """
        current = now or datetime.now(timezone.utc)
        findings: list[dict] = []
        for data_type, reference in references:
            horizon = self.policy.max_age_for(data_type)
            if horizon is None:
                continue
            moment = reference.observed_at
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            age = max(0, int((current - moment).total_seconds()))
            # Every finding carries the instant it was judged. A finding is stored with
            # the plan, so without this a verdict computed days ago would read as a
            # statement about now — the same confusion between "when we looked" and
            # "what is true" that this module exists to prevent. The absolute
            # observation time is included too, so a reader can recompute the age at any
            # later moment rather than trusting a frozen number.
            common = {
                "data_type": data_type,
                "source": reference.source,
                "source_id": reference.source_id,
                "observed_at_basis": str(reference.observed_at_basis),
                "observed_at": moment.isoformat(),
                "fetched_at": reference.fetched_at.isoformat(),
                "evaluated_at": current.isoformat(),
                "data_age_seconds": age,
                "max_age_seconds": horizon,
            }
            if not self.age_is_known(reference):
                findings.append(
                    {
                        **common,
                        "reason": "age_unknown",
                        "detail": (
                            "来源未提供数据自身的生成时间，因此只能确认「我们何时抓取」，"
                            "无法确认数据本身有多新"
                        ),
                    }
                )
                continue
            if age > horizon:
                findings.append(
                    {
                        **common,
                        "reason": "too_old",
                        # Anchored to the absolute observation time rather than to "N
                        # hours ago", because this sentence is stored and read later,
                        # when a relative phrasing would have quietly become wrong.
                        "detail": (
                            f"数据自身时间为 {moment.isoformat()}，超出该类事实可接受的 "
                            f"{horizon} 秒（实际早 {age - horizon} 秒），"
                            "不应作为当前信息呈现"
                        ),
                    }
                )
        return findings

    def stale_facts(
        self, references: list[tuple[str, SourceReference]], *, now: datetime | None = None
    ) -> list[dict]:
        """Only the findings that mean the data is genuinely too old.

        Separated from :meth:`audit` so a caller can fail loudly on real staleness
        without also failing on the ordinary, expected case of a source that does not
        publish observation times. Conflating the two would train a reader to ignore
        both.
        """
        return [
            finding for finding in self.audit(references, now=now) if finding["reason"] == "too_old"
        ]
