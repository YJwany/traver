"""The staleness verdict must survive storage, and must not age into a lie.

A finding is computed once, stored with the plan, and read back later — possibly days
later. That creates two failure modes worth pinning:

1. **loss** — if the preview were persisted field by field, a new field like
   ``stale_facts`` would be silently dropped, and the API would report "everything is
   within its horizon" for a plan that had been flagged. A missing warning is
   indistinguishable from no warning.
2. **drift** — the stored numbers were true when computed. The finding therefore has to
   carry the instant it was judged and the absolute observation time, so a reader can
   tell a fresh verdict from an old one instead of reading a frozen figure as current.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.db.session import Database
from travel_api.db.uow import UnitOfWork
from travel_api.domain.models import (
    BudgetBreakdown,
    DataDomain,
    ObservationBasis,
    SourceReference,
    SourceType,
    Trip,
    TripPreview,
    TripProfile,
)
from travel_api.main import create_app
from travel_api.services.freshness import FreshnessService


def _reference(*, observed_at: datetime, basis: ObservationBasis) -> SourceReference:
    now = datetime.now(timezone.utc)
    return SourceReference(
        source="amap-weather",
        source_type=SourceType.OFFICIAL_API,
        source_id="amap:weather:110000",
        observed_at=observed_at,
        fetched_at=now,
        expires_at=now + timedelta(seconds=300),
        observed_at_basis=basis,
        request_context={"cached": False},
        data_version="amap-live",
        confidence=0.8,
        domain=DataDomain.GEO_FACT,
    )


def _trip_with(
    findings: list[dict],
    references: list[SourceReference],
    *,
    unverifiable: list[dict] | None = None,
) -> Trip:
    profile = TripProfile(
        destination="北京",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 3),
        travelers=2,
        total_budget=Decimal("6000"),
    )
    return Trip(
        profile=profile,
        preview=TripPreview(
            profile=profile,
            hotels=[],
            hotel_offers=[],
            hotel_decisions=[],
            restaurants=[],
            attractions=[],
            route_legs=[],
            budget=BudgetBreakdown(
                items=[],
                total=Decimal("0"),
                budget_limit=None,
                remaining=None,
                over_budget=False,
            ),
            provider_health=[],
            source_references=references,
            stale_facts=findings,
            age_unknown_facts=unverifiable if unverifiable is not None else [],
        ),
    )


def test_a_staleness_finding_survives_the_database(tmp_path):
    """Storing the plan must not quietly discard the warning attached to it."""
    database = Database(f"sqlite:///{tmp_path / 'stale.db'}")
    database.create_all()

    now = datetime.now(timezone.utc)
    reference = _reference(
        basis=ObservationBasis.PROVIDER_REPORTED, observed_at=now - timedelta(days=3)
    )
    findings = FreshnessService().stale_facts([("weather", reference)], now=now)
    assert findings, "the fixture must actually be stale, or this test proves nothing"

    trip = _trip_with(findings, [reference])
    with UnitOfWork(database.session_factory) as uow:
        uow.trips.create(trip)

    with UnitOfWork(database.session_factory) as uow:
        reloaded = uow.trips.get(trip.id)

    assert reloaded is not None
    assert reloaded.preview is not None
    assert reloaded.preview.stale_facts == findings, (
        "the finding must come back exactly as recorded; a dropped field would make the "
        "API report a flagged plan as clean"
    )
    # The basis must survive too, or a reloaded plan loses the distinction between
    # "the source told us this is old" and "we do not know how old this is".
    assert reloaded.preview.source_references[0].observed_at_basis is (
        ObservationBasis.PROVIDER_REPORTED
    )


def test_a_reloaded_finding_still_discloses_when_it_was_judged(tmp_path):
    """A verdict computed yesterday must not read as a statement about today.

    The relative facts (``data_age_seconds``) are frozen at evaluation, which is
    correct as a historical record. What makes it safe is that ``evaluated_at`` and the
    absolute ``observed_at`` travel with it, so nothing has to be taken on trust.
    """
    database = Database(f"sqlite:///{tmp_path / 'evaluated.db'}")
    database.create_all()

    judged_at = datetime.now(timezone.utc) - timedelta(days=1)
    reference = _reference(
        basis=ObservationBasis.PROVIDER_REPORTED, observed_at=judged_at - timedelta(days=2)
    )
    findings = FreshnessService().stale_facts([("weather", reference)], now=judged_at)
    assert findings

    # Built once: Trip gets a fresh UUID at construction, so calling the helper twice
    # would look up a different trip than the one that was stored.
    trip = _trip_with(findings, [reference])
    with UnitOfWork(database.session_factory) as uow:
        uow.trips.create(trip)

    with UnitOfWork(database.session_factory) as uow:
        reloaded = uow.trips.get(trip.id)
    assert reloaded is not None

    finding = reloaded.preview.stale_facts[0]
    assert finding["evaluated_at"] == judged_at.isoformat()
    assert finding["observed_at"] == reference.observed_at.isoformat()
    assert finding["reason"] == "too_old"
    # The prose names the absolute time rather than saying "N hours ago", which would
    # have become wrong during the day it sat in the database.
    assert finding["observed_at"] in finding["detail"]
    assert "小时前" not in finding["detail"]


def test_a_clean_plan_stores_an_empty_list_and_says_so(tmp_path):
    """``stale_facts == []`` is a claim, so it must be recorded deliberately.

    An absent key and an empty list would look identical to a careless client; the
    field is always present so "nothing was flagged" is a positive statement rather
    than a silence that could also mean "the field was dropped".
    """
    database = Database(f"sqlite:///{tmp_path / 'clean.db'}")
    database.create_all()

    now = datetime.now(timezone.utc)
    reference = _reference(
        basis=ObservationBasis.PROVIDER_REPORTED, observed_at=now - timedelta(minutes=1)
    )
    assert FreshnessService().stale_facts([("weather", reference)], now=now) == []

    trip = _trip_with([], [reference])
    with UnitOfWork(database.session_factory) as uow:
        uow.trips.create(trip)
    with UnitOfWork(database.session_factory) as uow:
        reloaded = uow.trips.get(trip.id)

    assert reloaded.preview.stale_facts == []
    dumped = reloaded.preview.model_dump(mode="json")
    assert "stale_facts" in dumped


def test_an_unverifiable_fact_is_reachable_and_kept_apart_from_a_stale_one(tmp_path):
    """``age_unknown`` must survive to the client, and must not be filed as staleness.

    Both halves of this were once broken in the same direction. The audit computed
    ``age_unknown`` findings and then threw them away — only the too-old subset was assigned
    to ``stale_facts`` — so the API reported an empty staleness list for a plan where most
    sources publish no observation time at all, and an empty list reads as "verified
    current". The docs had claimed the two were reported separately the whole time.

    Kept apart rather than merged because the remedies differ: a stale fact is re-fetched or
    discarded, an unverifiable one can only be disclosed. Folding the second into the first
    would put a warning on every plan; dropping it, which is what happened, claimed a
    verification nobody performed.
    """
    database = Database(f"sqlite:///{tmp_path / 'unknown.db'}")
    database.create_all()

    now = datetime.now(timezone.utc)
    silent = _reference(
        basis=ObservationBasis.SOURCE_SILENT, observed_at=now - timedelta(minutes=1)
    )
    findings = FreshnessService().audit([("weather", silent)], now=now)
    assert [finding["reason"] for finding in findings] == ["age_unknown"]

    trip = _trip_with([], [silent], unverifiable=findings)
    with UnitOfWork(database.session_factory) as uow:
        uow.trips.create(trip)
    with UnitOfWork(database.session_factory) as uow:
        reloaded = uow.trips.get(trip.id)

    # It round-trips, so it can reach a reader.
    assert [finding["reason"] for finding in reloaded.preview.age_unknown_facts] == ["age_unknown"]
    # And it is not filed as staleness, which would be an assertion the source gave us no
    # grounds for.
    assert reloaded.preview.stale_facts == []


def test_both_freshness_lists_are_always_present_over_http(tmp_path):
    """A client must be able to tell "nothing flagged" from "the field was dropped".

    This is why the panel treats an absent ``age_unknown_facts`` as unknown rather than as
    empty: if the field could go missing, an empty reading would manufacture a verification.
    The API's job is to make absence impossible in the first place.
    """
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'both.db'}",
        auto_create_schema=True,
        auth_enabled=False,
        credential_key_file=str(tmp_path / "credential.key"),
        provider_retry_count=0,
        free_providers_enabled=False,
        mock_provider_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/trips",
            json={
                "destination": "成都",
                "start_date": "2026-10-03",
                "end_date": "2026-10-05",
                "travelers": 2,
                "total_budget": 5000,
            },
        )
        assert created.status_code == 200
        preview_payload = created.json()["preview"]

        assert "stale_facts" in preview_payload
        assert "age_unknown_facts" in preview_payload
        # The fixture's references carry no observation basis, so they are unverifiable —
        # which is exactly the state whose disclosure used to be missing.
        assert preview_payload["age_unknown_facts"], (
            "the deterministic fixture stamps no observation basis, so at least one "
            "reference must be reported as unverifiable"
        )
        for finding in preview_payload["age_unknown_facts"]:
            assert finding["reason"] == "age_unknown"
            # The disclosure has to name the source and both times, or it cannot be checked.
            assert finding["source"]
            assert finding["fetched_at"]
            assert finding["evaluated_at"]


def test_the_api_exposes_the_staleness_verdict_on_a_stored_trip(tmp_path):
    """The field has to be reachable from the HTTP response, not just the database.

    Without this the mechanism could work perfectly and still be invisible to the only
    consumer that matters.
    """
    from fastapi.testclient import TestClient
    from travel_api.config import Settings
    from travel_api.main import create_app

    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        auto_create_schema=True,
        provider_retry_count=0,
        free_providers_enabled=False,
        mock_provider_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/trips",
            json={
                "destination": "成都",
                "start_date": "2026-10-03",
                "end_date": "2026-10-05",
                "travelers": 2,
                "total_budget": 5000,
            },
        )
        assert created.status_code == 200
        trip = created.json()

        # Generated by the deterministic fixture plan, so nothing here should be stale.
        assert trip["preview"]["stale_facts"] == []

        reloaded = client.get(f"/api/v1/trips/{trip['id']}")
        assert reloaded.status_code == 200
        assert reloaded.json()["preview"]["stale_facts"] == []

        # And every reference states which kind of time its observed_at is.
        for reference in reloaded.json()["preview"]["source_references"]:
            assert reference["observed_at_basis"] in {
                "provider_reported",
                "live_computed",
                "source_silent",
                "unknown",
            }
            assert "data_age_seconds" in reference
