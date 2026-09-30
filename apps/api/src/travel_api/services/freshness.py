from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from travel_api.domain.models import ElementAgeKind, SourceReference


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
    # Eighteen months, and the number is the *reviewer's* date rather than a cache TTL.
    #
    # This is the one horizon in this table that exists because the user asked for it in so
    # many words: 「只能用近期的评价……不能是那种几年前那种」. It is not the same kind of fact as
    # the rows above. A restaurant listing being a day old is a statement about our copy; a
    # review being four years old is a statement about the *world* — the reviewer is
    # describing a place as it was before the last renovation, the last change of owner, or
    # the last two years of a neighbourhood. So the horizon is loose in absolute terms and
    # still does the work: it separates "people are still going and still writing about it"
    # from "this is an old memory that may describe somewhere that no longer exists".
    #
    # Not tighter, because reviews are sparse: a genuinely good restaurant in a small town may
    # have nothing newer, and a horizon of a few months would flag almost every review of
    # almost every place outside a capital, which is how a reader learns to ignore warnings.
    # Undated reviews are the harder case and are *not* covered by this number: they are
    # reported as age-unknown through the ``SOURCE_SILENT`` path, because assuming they are
    # recent is exactly the mistake this horizon exists to prevent.
    "review": 18 * 30 * 86400,
    # Deliberately absent: ``user_content``.
    #
    # A note the traveller supplied is judged by *its own* stated publication date, and
    # that date can be years old without the note being unusable — a well-written
    # neighbourhood guide does not expire on a schedule the way a forecast does. What
    # matters is that the age is *disclosed*, not that some threshold is enforced. Giving
    # it a horizon here would produce a warning on every genuinely useful old guide,
    # which is how a reader learns to ignore warnings. Undated notes are still reported
    # as age-unknown through the normal ``SOURCE_SILENT`` path.
    #
    # Deliberately absent: ``place_lookup`` — and this one needs saying out loud, because
    # the *reference* certainly exists. `aggregation.py` emits ``("place_lookup", ref)``
    # pairs for every name the notes mentioned that our sources did not already have, so a
    # missing key here silently means those citations are skipped by `audit` entirely.
    #
    # That silence is correct, but for a reason worth writing down rather than leaving to be
    # inferred from an omission. Geocoder responses are ``SOURCE_SILENT``: the index's rebuild
    # time is not published, so an ``observed_at`` for one of them is really our fetch time
    # wearing the wrong label (see `docs/architecture/content-ingestion.md`). With a horizon,
    # every single coordinate lookup would therefore produce an `age_unknown` finding — not
    # because anything is uncertain about where 西湖 is, but because we would have asked a
    # question the source never answers. A finding that can only ever read "unknown" carries no
    # information and would bury the ones that do.
    #
    # The distinction: ``user_content`` is absent because old notes are genuinely fine, and
    # *this* is absent because a place's location is not the kind of fact that goes stale at
    # all. Both are absences; they are not the same absence.
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
            # Whether the age of the *data* is knowable, from either source of information.
            #
            # `age_is_known` asks about `observed_at` alone. A source that publishes no batch
            # timestamp but does stamp its individual members has still told us how old the data
            # is — just per member rather than per payload. Reporting `age_unknown` then would be
            # false in the direction that matters: it would say "we cannot tell how old this is"
            # about a set whose ages we can read off directly, and the finding a reader needs
            # ("half of these were last edited years ago") would be the one withheld.
            element_age = reference.element_age
            if not self.age_is_known(reference) and element_age is None:
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
            # A batch timestamp that is knowable is judged first, because it is a statement
            # about the whole payload and is cheap to check.
            if self.age_is_known(reference) and age > horizon:
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
                continue
            # The single timestamp passed. If the source told us its members have their own
            # ages, judge those too — passing this point is not the same as the facts being
            # current.
            element_finding = self._element_age_finding(reference, common, horizon, moment)
            if element_finding is not None:
                findings.append(element_finding)
        return findings

    @staticmethod
    def _element_age_finding(
        reference: SourceReference, common: dict, horizon: int, snapshot: datetime
    ) -> dict | None:
        """Report a set whose members are older than their own horizon.

        This closes a hole the single-timestamp model cannot see. Overpass reports
        ``osm3s.timestamp_osm_base`` — when OSM cut the database — and it is genuinely
        provider-reported, so ``age_is_about_the_data`` is true and the check above passes. But
        that timestamp says nothing about the elements: measured against the live service, a
        snapshot **1 minute** old held elements whose median last edit was **914 days** earlier,
        with 19 of 46 over five years old.

        So a plan built on fresh snapshots could pass every freshness check while describing
        restaurants that closed years ago. That is precisely the failure the 时效性 requirement
        names — "不能是那种几年前那种" — and it was invisible because the word "fresh" was
        being applied to the wrong object.

        Reported as ``too_old`` with the distribution in the detail rather than a verdict,
        because the honest statement is a shape, not a yes/no: half the list is two years old
        and one entry is a decade old are different situations, and a reader deciding whether to
        trust a restaurant needs the second number.
        """
        element_age = reference.element_age
        if element_age is None or not element_age.any_older_than_horizon:
            return None
        # `observed_at` describes the batch; the age quoted here is about the members. Which of
        # the two the reader is looking at is the whole point, so the sentence names both and
        # says which one is which, including when the batch time is our own fetch time.
        batch = (
            f"该来源的数据自身时间（{snapshot.isoformat()}）"
            if reference.observed_at_basis.age_is_about_the_data
            else f"该来源未提供整批数据的时间（{snapshot.isoformat()} 是我们抓取的时刻）"
        )
        # The verb depends on what the members are. An OSM element is last *edited*; a review is
        # *written* once and never modified, so describing it as having been modified on some
        # later date would invent an event — and this sentence is stored with the plan and read
        # later, so it cannot lean on the reader knowing which capability produced it.
        made = "最后修改于" if element_age.member_kind is ElementAgeKind.EDITED else "写于"
        return {
            **common,
            "reason": "too_old",
            "element_age": element_age.model_dump(mode="json"),
            "detail": (
                f"{batch}描述的是整批数据，但其中 "
                f"{element_age.older_than_horizon} / {element_age.count} 条自身已超过"
                f"该类事实可接受的 {horizon} 秒：中位 {element_age.median_days} 天、"
                f"最早一条{made} {element_age.oldest_at.date().isoformat()}"
                f"（{round(element_age.oldest_seconds / 86400)} 天前）。"
                "单条时间戳无法表达这种分布，因此单独列出"
            ),
        }

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
