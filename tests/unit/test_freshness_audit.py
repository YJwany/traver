"""Tests for data-age honesty: what ``observed_at`` means, and when a fact is too old.

A trip is planned for a future date, so the age of a fact is not a detail. A
three-day-old forecast or a listing from two years ago is worse than a missing one:
it looks like knowledge. These tests pin the two failure modes that matter —

1. claiming a datum is fresher than it is, by stamping our fetch time on it when the
   source either told us otherwise or told us nothing;
2. flagging everything, which is the same as flagging nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from travel_api.domain.models import (
    DataDomain,
    ObservationBasis,
    SourceReference,
    SourceType,
)
from travel_api.providers.amap.weather import parse_report_time
from travel_api.services.freshness import MAX_AGE_SECONDS, FreshnessPolicy, FreshnessService


def _reference(
    *,
    basis: ObservationBasis,
    observed_at: datetime | None = None,
    source: str = "test-source",
) -> SourceReference:
    now = datetime.now(timezone.utc)
    return SourceReference(
        source=source,
        source_type=SourceType.OFFICIAL_API,
        source_id="test:1",
        observed_at=observed_at or now,
        fetched_at=now,
        expires_at=now + timedelta(seconds=300),
        observed_at_basis=basis,
        request_context={},
        data_version="test",
        confidence=1.0,
        domain=DataDomain.GEO_FACT,
    )


# -- parsing AMap's reporttime -------------------------------------------


def test_reporttime_is_read_as_beijing_time_not_utc():
    """The timezone trap, which fails in the dangerous direction.

    AMap sends ``"2025-05-01 08:00:00"`` with no zone, in China Standard Time. Reading
    it as UTC would place the publication eight hours in the *future*, so
    ``data_age_seconds`` would clamp to zero and a genuinely stale forecast would look
    freshly published — the one error that hides staleness instead of exposing it.
    """
    parsed = parse_report_time("2025-05-01 08:00:00")

    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=8)
    # 08:00 Beijing *is* 00:00 UTC — the same instant, labelled correctly.
    assert parsed == datetime(2025, 5, 1, 0, 0, tzinfo=timezone.utc)

    # The misreading this guards against: taking the text at face value as UTC. That
    # instant is eight hours later, which is what would make an old forecast look new.
    misread_as_utc = datetime(2025, 5, 1, 8, 0, tzinfo=timezone.utc)
    assert misread_as_utc - parsed == timedelta(hours=8)
    assert misread_as_utc != parsed


def test_an_unparseable_reporttime_is_none_rather_than_now():
    """Substituting the current time would silently claim the forecast is fresh.

    ``None`` forces the caller to decide, and the caller records that AMap was silent
    instead of pretending it reported something.
    """
    assert parse_report_time(None) is None
    assert parse_report_time("") is None
    assert parse_report_time("not a date") is None
    # A format AMap has not been observed to use is not guessed at.
    assert parse_report_time("01/05/2025") is None


def test_the_shorter_formats_amap_might_send_are_still_understood():
    """A date-only or minute-precision stamp is still usable, and still Beijing time."""
    for text in ("2025-05-01 08:00", "2025-05-01"):
        parsed = parse_report_time(text)
        assert parsed is not None, text
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(hours=8)


# -- what a basis claims --------------------------------------------------


@pytest.mark.parametrize(
    ("basis", "describes_the_data"),
    [
        (ObservationBasis.PROVIDER_REPORTED, True),
        (ObservationBasis.LIVE_COMPUTED, False),
        (ObservationBasis.SOURCE_SILENT, False),
        (ObservationBasis.UNKNOWN, False),
    ],
)
def test_only_a_provider_reported_time_says_anything_about_the_data(
    basis: ObservationBasis, describes_the_data: bool
):
    """``age_is_about_the_data`` is the guard against over-reading a timestamp.

    A live-computed route is genuinely fresh but says nothing about an underlying
    dataset; a silent source says nothing at all; an unclassified reference must never
    be treated as evidence of freshness. Only one basis is a claim about the data.
    """
    assert basis.age_is_about_the_data is describes_the_data


def test_an_unclassified_reference_is_not_treated_as_fresh_evidence():
    """The default must be the cautious value.

    If ``UNKNOWN`` counted as provider-reported, any reference a new provider forgot to
    classify would be reported as verifiably recent. Failing closed is the only safe
    default here.
    """
    reference = _reference(basis=ObservationBasis.UNKNOWN)
    assert FreshnessService().age_is_known(reference) is False


def test_data_age_is_never_negative():
    """Clock skew must not make a datum look fresher than fresh.

    AMap rounds publication times up, and travellers' machines are not in sync with
    ours. A negative age would subtract from the elapsed time and could pull a stale
    item back inside its horizon.
    """
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    reference = _reference(basis=ObservationBasis.PROVIDER_REPORTED, observed_at=future)

    assert reference.data_age_seconds == 0


def test_data_age_measures_the_data_not_the_fetch():
    """A just-fetched copy of an old dataset is old.

    This is the distinction the whole mechanism exists for: fetched a moment ago and
    observed two days ago are both true, and only the second decides whether the fact
    is worth anything.
    """
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    reference = _reference(basis=ObservationBasis.PROVIDER_REPORTED, observed_at=two_days_ago)

    assert reference.data_age_seconds >= 172800 - 5
    assert reference.is_stale is False, (
        "our copy is still within its validity window; the data's age is a separate fact"
    )


# -- the verdict ----------------------------------------------------------


def test_a_provider_reported_fact_past_its_horizon_is_reported_as_too_old():
    now = datetime.now(timezone.utc)
    reference = _reference(
        basis=ObservationBasis.PROVIDER_REPORTED,
        observed_at=now - timedelta(days=3),
    )

    findings = FreshnessService().audit([("weather", reference)], now=now)

    assert len(findings) == 1
    assert findings[0]["reason"] == "too_old"
    assert findings[0]["data_age_seconds"] > findings[0]["max_age_seconds"]
    assert findings[0]["data_type"] == "weather"


def test_a_silent_source_is_not_reported_as_too_old():
    """The two failures have different remedies and must stay distinguishable.

    "We cannot tell how old this is" is not "this is out of date". Conflating them
    would either cry wolf about every source that omits timestamps, or let a
    verifiably stale fact hide behind a vaguer label.
    """
    now = datetime.now(timezone.utc)
    reference = _reference(basis=ObservationBasis.SOURCE_SILENT, observed_at=now)

    findings = FreshnessService().audit([("restaurant", reference)], now=now)

    assert [item["reason"] for item in findings] == ["age_unknown"]
    assert findings[0]["observed_at_basis"] == "source_silent"
    # And the separate accessor must not surface it as staleness.
    assert FreshnessService().stale_facts([("restaurant", reference)], now=now) == []


def test_a_silent_source_old_fetch_is_still_only_age_unknown():
    """Even a reference fetched two days ago is "unknown", not "too old".

    We genuinely do not know: the datum could have been produced seconds before we
    asked. Reporting it as stale would assert something we cannot support, which is the
    same defect in the other direction.
    """
    now = datetime.now(timezone.utc)
    reference = _reference(
        basis=ObservationBasis.LIVE_COMPUTED,
        observed_at=now - timedelta(days=2),
    )

    findings = FreshnessService().audit([("route", reference)], now=now)

    assert [item["reason"] for item in findings] == ["age_unknown"]


def test_a_recent_provider_reported_fact_produces_no_finding():
    """The negative case. A check that always fires trains readers to ignore it."""
    now = datetime.now(timezone.utc)
    reference = _reference(
        basis=ObservationBasis.PROVIDER_REPORTED,
        observed_at=now - timedelta(minutes=5),
    )

    assert FreshnessService().audit([("weather", reference)], now=now) == []


def test_a_data_type_with_no_horizon_is_not_judged_at_all():
    """Silence is better than a verdict we cannot justify.

    Nothing here claims to know how fast a Wikipedia article goes out of date, so no
    finding is emitted — neither "fine" nor "stale".
    """
    now = datetime.now(timezone.utc)
    reference = _reference(
        basis=ObservationBasis.PROVIDER_REPORTED,
        observed_at=now - timedelta(days=3650),
    )

    assert FreshnessService().audit([("wikipedia_article", reference)], now=now) == []
    assert FreshnessPolicy().max_age_for("wikipedia_article") is None


def test_the_weather_horizon_is_tighter_than_a_restaurant_listing():
    """The horizons must reflect how fast each kind of fact actually changes.

    Encoding this in one policy is what keeps the rule consistent between the planner,
    the API and the UI, instead of each judging "recent enough" for itself.
    """
    policy = FreshnessPolicy()

    assert policy.max_age_for("weather") < policy.max_age_for("restaurant")
    assert policy.max_age_for("weather") < policy.max_age_for("attraction")
    # Availability is the fact most likely to change while a traveller is looking at it.
    assert policy.max_age_for("hotel_availability") < policy.max_age_for("hotel_offer")
    assert set(MAX_AGE_SECONDS) >= {"weather", "route", "restaurant", "attraction", "hotel"}


def test_an_operator_can_override_a_horizon():
    """A deployment may know better than the default for its own domain."""
    policy = FreshnessPolicy(max_age={"weather": 60})
    service = FreshnessService(policy)
    now = datetime.now(timezone.utc)
    reference = _reference(
        basis=ObservationBasis.PROVIDER_REPORTED,
        observed_at=now - timedelta(minutes=5),
    )

    findings = service.audit([("weather", reference)], now=now)

    assert [item["reason"] for item in findings] == ["too_old"]
    # Unlisted types keep the default rather than losing their horizon.
    assert policy.max_age_for("restaurant") == MAX_AGE_SECONDS["restaurant"]


def test_every_finding_carries_both_times_and_a_readable_reason():
    """The user-facing requirement: say when it was fetched *and* how old it is.

    A finding without both times cannot be acted on, and one without prose cannot be
    shown. The detail is written for a reader, not for a log.
    """
    now = datetime.now(timezone.utc)
    reference = _reference(
        basis=ObservationBasis.PROVIDER_REPORTED,
        observed_at=now - timedelta(days=2),
        source="amap-weather",
    )

    finding = FreshnessService().audit([("weather", reference)], now=now)[0]

    assert finding["source"] == "amap-weather"
    assert finding["source_id"] == "test:1"
    assert finding["observed_at"] and finding["fetched_at"]
    assert finding["data_age_seconds"] and finding["max_age_seconds"]
    # The verdict is stamped with when it was computed, so a stored finding cannot be
    # mistaken for a statement about the present.
    assert finding["evaluated_at"]
    # Anchored to the absolute observation time, because the sentence is stored and
    # read later, by which point a relative phrasing would have become wrong.
    assert finding["observed_at"] in finding["detail"]
