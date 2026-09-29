from datetime import datetime, timedelta, timezone

from travel_api.domain.models import (
    DataDomain,
    ElementAge,
    ObservationBasis,
    SourceReference,
    SourceType,
)
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


def _reference_with_element_age(
    now: datetime,
    *,
    snapshot_seconds_ago: int,
    median_days: float,
    oldest_days: float,
    over_horizon: int,
    count: int = 46,
) -> SourceReference:
    return SourceReference(
        source="openstreetmap",
        source_type=SourceType.OPEN_DATA,
        source_id="overpass:30.23,120.13,30.27,120.17",
        observed_at=now - timedelta(seconds=snapshot_seconds_ago),
        observed_at_basis=ObservationBasis.PROVIDER_REPORTED,
        fetched_at=now,
        element_age=ElementAge(
            count=count,
            median_seconds=int(median_days * 86400),
            oldest_seconds=int(oldest_days * 86400),
            oldest_at=now - timedelta(days=oldest_days),
            older_than_horizon=over_horizon,
            horizon_seconds=86400,
            computed_at=now,
        ),
        domain=DataDomain.GEO_FACT,
    )


def test_a_fresh_snapshot_of_old_elements_is_not_reported_as_fresh_data():
    """The hole the single-timestamp model cannot see, closed.

    ``osm3s.timestamp_osm_base`` is genuinely provider-reported, so the age check on it passes:
    ``observed_at`` is the snapshot instant, ``age_is_about_the_data`` is true, and nothing is
    reported. But the snapshot describes *our copy of the database*, not the facts in it.

    Measured against the live service, 杭州西湖's snapshot was 1 minute old while the median
    element had last been edited 914 days earlier and 19 of 46 were over five years old. So a
    plan built entirely from snapshots taken this minute passed every freshness check while
    listing places that may have closed years ago — the exact failure the 时效性 requirement
    names, hidden behind the word "fresh" being applied to the wrong object.
    """
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    service = FreshnessService()
    reference = _reference_with_element_age(
        now, snapshot_seconds_ago=60, median_days=914, oldest_days=5638, over_horizon=46
    )

    findings = service.audit([("attraction", reference)], now=now)
    assert findings, (
        "a reference whose elements are years old produced no finding, so the plan presents "
        "them as current"
    )
    finding = findings[0]
    assert finding["reason"] == "too_old"
    # The finding says which of the two times it is about, because conflating them is the bug.
    assert "该来源的数据自身时间" in finding["detail"]
    assert "46 / 46" in finding["detail"]
    assert "914" in finding["detail"] or "914.0" in finding["detail"]
    # And the distribution travels with it, structured, so a client need not parse prose.
    assert finding["element_age"]["median_seconds"] == 914 * 86400
    assert finding["element_age"]["older_than_horizon"] == 46
    assert finding["element_age"]["any_older_than_horizon"] is True


def test_a_set_whose_members_are_all_current_is_not_reported():
    """The guard against a finding that always fires.

    If every element is inside its horizon, saying nothing is correct — and a check that
    reported the distribution regardless would be noise that trains a reader to ignore the
    banner, which is the same failure as reporting nothing at all.
    """
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    service = FreshnessService()
    reference = _reference_with_element_age(
        now, snapshot_seconds_ago=60, median_days=0.3, oldest_days=0.9, over_horizon=0
    )

    assert service.audit([("attraction", reference)], now=now) == []


def test_a_reference_without_element_age_is_judged_as_before():
    """Absent is not old, and it is not fresh either — it is simply not known here.

    Most sources do report one production time for the whole payload, and those must keep
    being judged by it. ``element_age`` is an *extra* fact for sources that have no single one,
    so its absence must not change the verdict for everyone else.
    """
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    service = FreshnessService()

    fresh = SourceReference(
        source="overpass",
        source_type=SourceType.OPEN_DATA,
        source_id="overpass:bbox",
        observed_at=now - timedelta(seconds=60),
        observed_at_basis=ObservationBasis.PROVIDER_REPORTED,
        fetched_at=now,
        domain=DataDomain.GEO_FACT,
    )
    assert fresh.element_age is None
    assert service.audit([("attraction", fresh)], now=now) == []

    stale = fresh.model_copy(update={"observed_at": now - timedelta(days=400)})
    findings = service.audit([("attraction", stale)], now=now)
    assert [finding["reason"] for finding in findings] == ["too_old"]
    # The single-timestamp wording, not the distribution one: the finding must describe the
    # check that actually produced it.
    assert "该来源的数据自身时间" not in findings[0]["detail"]
    assert "element_age" not in findings[0]


def test_a_source_silent_about_its_snapshot_still_reports_old_elements():
    """Knowing the members' ages makes "age unknown" the wrong answer.

    A source can be silent about the batch while stamping each member. ``age_is_known`` asks
    only about ``observed_at``, so it would say "we cannot tell how old this is" — about a set
    whose ages are written on every element. Worse, it would withhold the finding that matters:
    that the members are years old.

    The batch timestamp is still reported as unverifiable in the reference itself
    (``observed_at_basis`` is ``SOURCE_SILENT``, and the export/source table prints that), so
    nothing is lost by not duplicating it as a finding here.
    """
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    service = FreshnessService()
    reference = _reference_with_element_age(
        now, snapshot_seconds_ago=0, median_days=914, oldest_days=5638, over_horizon=10
    ).model_copy(update={"observed_at_basis": ObservationBasis.SOURCE_SILENT})

    findings = service.audit([("attraction", reference)], now=now)
    assert [finding["reason"] for finding in findings] == ["too_old"]
    finding = findings[0]
    # The sentence must not imply the batch time came from the source, because it did not.
    assert "是我们抓取的时刻" in finding["detail"]
    assert finding["element_age"]["median_seconds"] == 914 * 86400


def test_a_silent_source_with_recent_elements_reports_nothing():
    """And the other half: if the members are recent, we do know the data is recent.

    Emitting ``age_unknown`` here would be the conservative-looking mistake this codebase keeps
    finding — a sentence that sounds careful while deleting the actual answer. The batch
    timestamp is unknown; the data's age is not, and it is fine.
    """
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    service = FreshnessService()
    reference = _reference_with_element_age(
        now, snapshot_seconds_ago=0, median_days=0.2, oldest_days=0.5, over_horizon=0
    ).model_copy(update={"observed_at_basis": ObservationBasis.SOURCE_SILENT})

    assert service.audit([("attraction", reference)], now=now) == []


def test_a_silent_source_with_no_element_age_is_still_reported_as_age_unknown():
    """The pre-existing behaviour, kept: absent metadata is genuinely not knowable."""
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    service = FreshnessService()
    reference = SourceReference(
        source="overpass",
        source_type=SourceType.OPEN_DATA,
        source_id="overpass:bbox",
        observed_at=now,
        observed_at_basis=ObservationBasis.SOURCE_SILENT,
        fetched_at=now,
        domain=DataDomain.GEO_FACT,
    )

    findings = service.audit([("attraction", reference)], now=now)
    assert [finding["reason"] for finding in findings] == ["age_unknown"]
