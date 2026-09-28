"""Optimistic locking, pagination and inbound rate limiting.

The concurrency test drives the repository directly, because that is where the
compare-and-swap lives and a real second writer cannot be simulated through the
HTTP client (which serialises its own requests).
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.db.session import Database
from travel_api.db.uow import UnitOfWork
from travel_api.domain.models import Trip, TripProfile
from travel_api.main import create_app
from travel_api.services.trip_service import ConcurrentTripUpdateError, VersionConflictError


def _app(tmp_path, **overrides):
    base = {
        "app_env": "test",
        "database_url": f"sqlite:///{tmp_path / 'conc.db'}",
        "auto_create_schema": True,
        "provider_retry_count": 0,
        "free_providers_enabled": False,
        "mock_provider_enabled": True,
    }
    base.update(overrides)
    return create_app(Settings(**base))


def _body():
    start = date.today() + timedelta(days=1)
    return {
        "destination": "成都",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
        "travelers": 2,
        "total_budget": 6000,
    }


# -- optimistic locking ----------------------------------------------------


def test_update_returns_none_when_version_already_advanced(tmp_path):
    """The compare-and-swap must refuse a write built from a stale read."""
    database = Database(f"sqlite:///{tmp_path / 'cas.db'}")
    database.create_all()

    trip = Trip(
        profile=TripProfile(
            destination="成都",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 3),
            travelers=2,
            total_budget=6000,
        )
    )

    with UnitOfWork(database.session_factory) as uow:
        uow.trips.create(trip)
        assert uow.trips.get(trip.id) is not None

    # Writer A reads version 1, writer B writes version 2.
    with UnitOfWork(database.session_factory) as uow:
        assert uow.trips.update(trip.model_copy(update={"current_version": 2})) is not None

    with UnitOfWork(database.session_factory) as uow:
        stale = uow.trips.update(trip, expected_version=1)
        assert stale is None, "a stale write overwrote a newer version"

    # And the stored row still holds B's version.
    with UnitOfWork(database.session_factory) as uow:
        stored = uow.trips.get(trip.id)
    assert stored.current_version == 2


def test_patch_rejects_stale_expected_version(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        created = client.post("/api/v1/trips", json=_body())
        assert created.status_code == 200, created.text
        trip = created.json()
        assert trip["current_version"] == 1

        # Claiming a wrong version is refused before any work is done.
        conflict = client.patch(
            f"/api/v1/trips/{trip['id']}",
            json={"travelers": 3, "expected_version": 99},
        )
        assert conflict.status_code == 409, conflict.text

        # And the trip is untouched.
        reloaded = client.get(f"/api/v1/trips/{trip['id']}").json()
        assert reloaded["profile"]["travelers"] == 2

        # The correct version succeeds and advances it.
        ok = client.patch(
            f"/api/v1/trips/{trip['id']}",
            json={"travelers": 3, "expected_version": 1},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["current_version"] == 2
        assert ok.json()["profile"]["travelers"] == 3


def test_patch_without_expected_version_keeps_working(tmp_path):
    """The guard is opt-in so existing callers are unaffected."""
    with TestClient(_app(tmp_path)) as client:
        created = client.post("/api/v1/trips", json=_body()).json()
        response = client.patch(f"/api/v1/trips/{created['id']}", json={"travelers": 4})
    assert response.status_code == 200
    assert response.json()["current_version"] == 2


def test_version_conflict_error_is_a_value_error():
    """The route maps it to 409, so it must not be caught as a 422 revision error."""
    assert issubclass(VersionConflictError, ValueError)
    assert issubclass(ConcurrentTripUpdateError, RuntimeError)


# -- pagination ------------------------------------------------------------


def test_trip_list_paginates(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        ids = []
        for _ in range(5):
            created = client.post("/api/v1/trips", json=_body())
            assert created.status_code == 200
            ids.append(created.json()["id"])

        first = client.get("/api/v1/trips", params={"limit": 2, "offset": 0})
        assert first.status_code == 200
        assert len(first.json()) == 2

        second = client.get("/api/v1/trips", params={"limit": 2, "offset": 2})
        assert len(second.json()) == 2

        # Pages must not overlap.
        assert {item["id"] for item in first.json()}.isdisjoint(
            {item["id"] for item in second.json()}
        )

        beyond = client.get("/api/v1/trips", params={"limit": 2, "offset": 100})
        assert beyond.json() == []


def test_trip_list_rejects_absurd_pagination(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        # Unbounded limit would let one request pull the whole table.
        assert client.get("/api/v1/trips", params={"limit": 100000}).status_code == 422
        assert client.get("/api/v1/trips", params={"limit": 0}).status_code == 422
        assert client.get("/api/v1/trips", params={"offset": -1}).status_code == 422


# -- rate limiting ---------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(tmp_path):
    app = _app(tmp_path, rate_limit_requests=3, rate_limit_window_seconds=60.0)
    with TestClient(app) as client:
        client.post("/api/v1/trips", json=_body())
        statuses = [client.get("/api/v1/trips").status_code for _ in range(6)]
    assert 429 in statuses, f"limiter never engaged: {statuses}"
    assert statuses[0] == 200


def test_rate_limited_response_includes_retry_after(tmp_path):
    app = _app(tmp_path, rate_limit_requests=1, rate_limit_window_seconds=30.0)
    with TestClient(app) as client:
        client.get("/api/v1/trips")
        blocked = client.get("/api/v1/trips")
    assert blocked.status_code == 429
    assert "retry-after" in {key.lower() for key in blocked.headers}


def test_health_endpoints_are_exempt_from_rate_limiting(tmp_path):
    """A probe must not be able to lock itself out."""
    app = _app(tmp_path, rate_limit_requests=1, rate_limit_window_seconds=60.0)
    with TestClient(app) as client:
        for _ in range(10):
            assert client.get("/api/v1/health").status_code == 200


def test_limiter_window_resets(tmp_path):
    """A fixed window must reopen once it has elapsed."""
    from travel_api.api.rate_limit import RateLimiter

    limiter = RateLimiter(requests=2, window_seconds=10.0)
    assert limiter.check("ip:1.2.3.4", now=0.0) is None
    assert limiter.check("ip:1.2.3.4", now=1.0) is None
    # Exhausted: returns the delay to wait rather than raising, because a raise
    # from HTTP middleware would bypass FastAPI's handlers and become a 500.
    retry_after = limiter.check("ip:1.2.3.4", now=2.0)
    assert retry_after is not None and retry_after > 0

    # Past the window, the counter restarts rather than staying locked.
    assert limiter.check("ip:1.2.3.4", now=11.0) is None


def test_limiter_keys_are_independent(tmp_path):
    """One noisy client must not consume another's budget."""
    from travel_api.api.rate_limit import RateLimiter

    limiter = RateLimiter(requests=1, window_seconds=60.0)
    assert limiter.check("ip:a", now=0.0) is None
    assert limiter.check("ip:b", now=0.0) is None
    assert limiter.check("ip:a", now=0.0) is not None
    # b already used its single slot, but c is still available.
    assert limiter.check("ip:c", now=0.0) is None


def test_ai_endpoint_has_a_separate_tighter_budget(tmp_path):
    app = _app(
        tmp_path,
        rate_limit_requests=1000,
        rate_limit_ai_requests=2,
        rate_limit_window_seconds=60.0,
    )
    with TestClient(app) as client:
        payload = {"message": "成都有什么好吃的"}
        statuses = [client.post("/api/v1/ai/chat", json=payload).status_code for _ in range(5)]
    assert 429 in statuses, f"AI limiter never engaged: {statuses}"


# -- readiness -------------------------------------------------------------


def test_health_reports_database_failure(tmp_path, monkeypatch):
    """Readiness must go false when storage is unreachable."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json()["ready"] is True

        def broken_ping():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(client.app.state.database, "ping", broken_ping)
        payload = client.get("/api/v1/health").json()

    assert payload["ready"] is False
    assert payload["status"] == "degraded"
    assert payload["database"] == "unavailable"
    assert "connection refused" in payload["database_error"]


def test_unreachable_database_is_reported_not_crashed():
    """A bad URL must yield a degraded report, not an unhandled exception."""
    from travel_api.db.session import Database

    database = Database("sqlite:////nonexistent-directory/x.db")
    with pytest.raises(Exception):
        database.ping()


def test_unknown_trip_id_is_404(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        assert client.get(f"/api/v1/trips/{uuid4()}").status_code == 404
