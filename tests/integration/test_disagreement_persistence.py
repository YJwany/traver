"""A recorded disagreement has to survive storage and reach the HTTP response.

The merge records conflicts; that is only the first link. Two more can break the chain
silently, and both failures look identical to success from the outside:

1. **loss on write.** The plan is persisted as one JSON aggregate. If that ever became a
   field-by-field projection, a new field like ``disagreements`` would be dropped and the
   API would serve a conflict-free entity — one number beside several source names, which
   is precisely the fabricated consensus the field exists to prevent. A missing warning is
   indistinguishable from no warning.
2. **unreachable from the API.** A mechanism that works perfectly and never appears in the
   response the page renders has no effect on anyone. The field has to be on the payload
   the trip workspace actually fetches.

The distinction with an empty list is pinned too: ``disagreements == []`` must be present
and must mean "the sources that spoke agreed", not "the field was dropped".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from travel_api.db.session import Database
from travel_api.db.uow import UnitOfWork
from travel_api.domain.models import (
    Attraction,
    BudgetBreakdown,
    Coordinates,
    DataDomain,
    FieldClaim,
    FieldDisagreement,
    Location,
    Money,
    ObservationBasis,
    SourceReference,
    SourceType,
    Trip,
    TripPreview,
    TripProfile,
)
from travel_api.services.entity_resolution import deduplicate_attractions

NOW = datetime.now(timezone.utc)
BEFORE = NOW - timedelta(minutes=30)


def _reference(source: str, *, observed_at: datetime, source_id: str) -> SourceReference:
    return SourceReference(
        source=source,
        source_type=SourceType.OPEN_DATA if source == "openstreetmap" else SourceType.OFFICIAL_API,
        source_id=source_id,
        observed_at=observed_at,
        fetched_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        observed_at_basis=ObservationBasis.PROVIDER_REPORTED,
        request_context={},
        data_version="test",
        confidence=0.8,
        domain=DataDomain.GEO_FACT,
    )


def _attraction(source: str, *, ticket: str, observed_at: datetime) -> Attraction:
    reference = _reference(source, observed_at=observed_at, source_id=f"{source}-west-lake")
    return Attraction(
        canonical_name="西湖",
        location=Location(
            address="杭州市西湖区",
            city="杭州",
            coordinates=Coordinates(latitude=30.2489, longitude=120.1496),
            source_reference_id=reference.source_id,
        ),
        ticket_price=Money(
            value=Decimal(ticket), currency="CNY", source_reference_id=reference.source_id
        ),
        source_references=[reference],
    )


def _merged_attraction() -> Attraction:
    """The real merge output, so the fixture cannot drift from what the pipeline builds."""
    merged = deduplicate_attractions(
        [
            _attraction("openstreetmap", ticket="60", observed_at=BEFORE),
            _attraction("amap", ticket="80", observed_at=NOW),
        ]
    )
    assert len(merged) == 1
    assertion_guard = merged[0]
    assert assertion_guard.disagreements, "the fixture must actually disagree"
    return assertion_guard


def _trip_with(attraction: Attraction) -> Trip:
    profile = TripProfile(
        destination="杭州",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
        total_budget=Decimal("5000"),
    )
    return Trip(
        profile=profile,
        preview=TripPreview(
            profile=profile,
            hotels=[],
            hotel_offers=[],
            hotel_decisions=[],
            restaurants=[],
            attractions=[attraction],
            route_legs=[],
            budget=BudgetBreakdown(
                items=[],
                total=Decimal("0"),
                budget_limit=None,
                remaining=None,
                over_budget=False,
            ),
            provider_health=[],
            source_references=list(attraction.source_references),
        ),
    )


def test_a_disagreement_survives_the_database(tmp_path):
    """Storing the plan must not quietly discard the conflict attached to it."""
    database = Database(f"sqlite:///{tmp_path / 'disagreement.db'}")
    database.create_all()

    trip = _trip_with(_merged_attraction())
    with UnitOfWork(database.session_factory) as uow:
        uow.trips.create(trip)

    with UnitOfWork(database.session_factory) as uow:
        reloaded = uow.trips.get(trip.id)

    assert reloaded is not None and reloaded.preview is not None
    stored = reloaded.preview.attractions[0]
    assert stored.disagreements, "the finding must come back, or the page shows false consensus"

    finding = stored.disagreements[0]
    assert finding.field == "ticket_price"
    assert finding.chosen_source == "amap"
    assert finding.chosen_value_text == "¥80"
    assert finding.chosen_reason, "the rule that chose the value must survive too"
    assert {claim.source for claim in finding.claims} == {"openstreetmap", "amap"}
    assert {claim.value_text for claim in finding.claims} == {"¥60", "¥80"}
    # The times are what justify the choice; without them the retained value is asserted
    # rather than explained.
    assert all(claim.observed_at for claim in finding.claims)
    # And the value the entity kept is the chosen claim's, not the discarded one.
    assert stored.ticket_price is not None
    assert stored.ticket_price.value == Decimal("80")


def test_an_undisputed_entity_stores_an_empty_list_and_says_so(tmp_path):
    """``[]`` is a claim about the sources, so it must be present rather than absent.

    An absent key and an empty list look identical to a careless client, and the whole
    point of the field is that "the sources agreed" should be readable as such.
    """
    database = Database(f"sqlite:///{tmp_path / 'clean.db'}")
    database.create_all()

    agreed = deduplicate_attractions(
        [
            _attraction("openstreetmap", ticket="60", observed_at=BEFORE),
            _attraction("amap", ticket="60", observed_at=NOW),
        ]
    )[0]
    assert agreed.disagreements == []

    trip = _trip_with(agreed)
    with UnitOfWork(database.session_factory) as uow:
        uow.trips.create(trip)
    with UnitOfWork(database.session_factory) as uow:
        reloaded = uow.trips.get(trip.id)

    assert reloaded.preview.attractions[0].disagreements == []
    dumped = reloaded.preview.model_dump(mode="json")
    assert "disagreements" in dumped["attractions"][0], (
        "the key must be present and empty, so 'agreed' is not confused with 'dropped'"
    )


def test_the_api_exposes_a_disagreement_on_a_stored_trip(tmp_path):
    """The field has to be reachable from the HTTP response the workspace renders.

    Driven through the public API rather than the repository, because the failure this
    guards against is the mechanism being invisible to its only consumer.
    """
    from fastapi.testclient import TestClient
    from travel_api.config import Settings
    from travel_api.main import create_app

    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    settings = Settings(
        app_env="test",
        database_url=database_url,
        auto_create_schema=True,
        provider_retry_count=0,
        free_providers_enabled=False,
        mock_provider_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        key, _ = app.state.auth_service.mint("owner-a", label="owner-a")
        auth = {"Authorization": f"Bearer {key}"}
        created = client.post(
            "/api/v1/trips",
            headers=auth,
            json={
                "destination": "成都",
                "start_date": "2026-10-03",
                "end_date": "2026-10-05",
                "travelers": 2,
                "total_budget": 5000,
            },
        )
        assert created.status_code in {200, 201}, created.text
        trip_id = created.json()["id"]

    # Replace one attraction's findings with a real merged conflict, through the same
    # persistence path a plan takes, then read it back over HTTP.
    from travel_api.db.session import Database
    from travel_api.db.uow import UnitOfWork

    database = Database(database_url)
    with UnitOfWork(database.session_factory) as uow:
        stored = uow.trips.get(__import__("uuid").UUID(trip_id))
        merged = _merged_attraction()
        merged.id = stored.preview.attractions[0].id
        stored.preview.attractions[0] = merged
        stored.preview.source_references = [
            *stored.preview.source_references,
            *merged.source_references,
        ]
        assert uow.trips.update(stored) is not None

    with TestClient(create_app(settings)) as client:
        key = app.state.auth_service.mint("owner-a", label="owner-a")[0]
        response = client.get(
            f"/api/v1/trips/{trip_id}", headers={"Authorization": f"Bearer {key}"}
        )
        assert response.status_code == 200, response.text

    payload = response.json()["preview"]["attractions"]
    carrying = [item for item in payload if item.get("disagreements")]
    assert carrying, "the disagreement must appear on the endpoint the page fetches"

    finding = carrying[0]["disagreements"][0]
    for field in (
        "field",
        "claims",
        "chosen_source",
        "chosen_value_text",
        "chosen_reason",
    ):
        assert field in finding, f"{field} missing from the served payload"
    assert len(finding["claims"]) == 2
    assert all(claim.get("observed_at") for claim in finding["claims"])


def test_a_nested_merge_does_not_misattribute_the_merged_value():
    """Merging an already-merged entity must not credit its value to the wrong source.

    A merged entity's scalar no longer pairs with any single reference: it holds the
    winning claim's value while ``source_references`` lists every source that spoke. Reading
    a claim from it would credit the retained ¥80 to openstreetmap — which actually said
    ¥60 — and inherit openstreetmap's timestamp, making the wrong value look both newest and
    correctly attributed.

    The honest boundary is that these functions take freshly collected, single-source
    entities. This pins the guard that makes a nested merge degrade safely instead of
    inventing an attribution, which matters because "wrong number, confident label" is the
    exact failure this whole mechanism exists to prevent.
    """
    first = _merged_attraction()  # openstreetmap said ¥60, amap said ¥80; ¥80 retained

    merged_again = deduplicate_attractions(
        [first, _attraction("user-content", ticket="80", observed_at=NOW + timedelta(minutes=5))]
    )

    assert len(merged_again) == 1
    finding = next(d for d in merged_again[0].disagreements if d.field == "ticket_price")

    # The inherited finding is preserved exactly: two claims, correctly attributed.
    assert {claim.value_text: claim.source for claim in finding.claims} == {
        "¥60": "openstreetmap",
        "¥80": "amap",
    }
    # Nothing was relabelled: openstreetmap must not appear as the source of ¥80.
    assert not any(
        claim.value_text == "¥80" and claim.source == "openstreetmap" for claim in finding.claims
    )
    # And references are still de-duplicated, so a re-merge cannot inflate the source list
    # into apparent corroboration from more sources than actually spoke.
    assert len(merged_again[0].source_references) == 3


def test_a_freshly_collected_entity_is_still_merged_normally():
    """The guard must not blunt the normal path — that is what the pipeline actually runs."""
    merged = deduplicate_attractions(
        [
            _attraction("openstreetmap", ticket="60", observed_at=BEFORE),
            _attraction("amap", ticket="80", observed_at=NOW),
            _attraction("user-content", ticket="60", observed_at=NOW + timedelta(minutes=5)),
        ]
    )

    finding = next(d for d in merged[0].disagreements if d.field == "ticket_price")
    assert len(finding.claims) == 3
    assert set(finding.disputed_values) == {"¥60", "¥80"}


def test_a_hand_built_finding_round_trips_unchanged(tmp_path):
    """The stored shape is the contract with the UI, so it must be exact.

    Built by hand rather than by the merge, so a future change to the merge cannot make
    this test pass by coincidence.
    """
    database = Database(f"sqlite:///{tmp_path / 'exact.db'}")
    database.create_all()

    finding = FieldDisagreement(
        field="rating",
        chosen_source="amap",
        chosen_value_text="3.0 分",
        chosen_reason="取观测时间最新的一条（amap）",
        claims=[
            FieldClaim(
                source="openstreetmap",
                source_id="osm-1",
                value_text="4.5 分",
                observed_at=BEFORE,
                fetched_at=NOW,
            ),
            FieldClaim(
                source="amap",
                source_id="amap-1",
                value_text="3.0 分",
                observed_at=NOW,
                fetched_at=NOW,
            ),
        ],
    )
    attraction = _attraction("amap", ticket="80", observed_at=NOW)
    attraction.disagreements = [finding]

    trip = _trip_with(attraction)
    with UnitOfWork(database.session_factory) as uow:
        uow.trips.create(trip)
    with UnitOfWork(database.session_factory) as uow:
        reloaded = uow.trips.get(trip.id)

    assert reloaded.preview.attractions[0].disagreements == [finding]
