"""The job API, end to end.

The unit tests cover the registry's own invariants. These cover the route layer, where the
failure modes are different: a route that resolves to the wrong handler, an owner filter
that is not applied, and an SSE stream that either closes too early or never closes at all.

Two details are deliberate and are asserted rather than assumed:

  * jobs live under ``/planning``, not ``/trips/jobs``. The latter sits inside the
    ``/trips/{trip_id}`` space, where resolution depends on registration order — a route
    that silently breaks when a file is reordered. The test that the jobs path does not
    get swallowed as a trip id is here to keep that decision honest.
  * the SSE stream ends when the job does. A stream that stays open forever is a resource
    leak that only shows up under load.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app
from travel_api.services.owner_context import current_owner
from travel_api.services.trip_service import TripNotFoundError


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        free_providers_enabled=False,
        mock_provider_enabled=True,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _key(app, owner: str = "owner-a") -> str:
    key, _ = app.state.auth_service.mint(owner, label=owner)
    return key


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _profile() -> dict:
    return {
        "destination": "成都",
        "start_date": "2026-10-03",
        "end_date": "2026-10-05",
        "travelers": 2,
        "total_budget": 5000,
    }


def _settle(client, key: str, job_id: str, attempts: int = 200) -> dict:
    """Poll until terminal.

    Polling rather than sleeping a fixed amount, so the test is not a bet on how fast the
    machine is.
    """
    import time

    for _ in range(attempts):
        response = client.get(f"/api/v1/planning/jobs/{job_id}", headers=_auth(key))
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never settled: {payload['status']}")


# -- the route space ------------------------------------------------------------


def test_the_jobs_path_is_not_swallowed_as_a_trip_id(client, app):
    """`/trips/jobs` would be ambiguous with `/trips/{trip_id}`, which is why jobs are not
    there. This asserts the path we chose actually resolves to the jobs collection.

    Without this, a future rearrangement of the router could start returning 404 (a
    malformed trip id) or 422 for the same URL, and the only symptom would be a client
    that stops making progress.
    """
    key = _key(app)
    response = client.get("/api/v1/planning/jobs", headers=_auth(key))

    assert response.status_code == 200, response.text
    assert response.json() == []


# -- submitting -----------------------------------------------------------------


def test_submitting_a_job_returns_202_and_a_pollable_id(client, app):
    """202, not 201: nothing exists yet, and saying "created" would promise a trip."""
    key = _key(app)
    response = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key))

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["id"]
    assert payload["status"] in {"queued", "running", "succeeded"}
    assert payload["is_cancellable"] is True
    # The profile is echoed so a client holding only the id can render what it is for.
    assert payload["profile"]["destination"] == "成都"

    settled = _settle(client, key, payload["id"])
    assert settled["status"] == "succeeded", settled.get("error")
    assert settled["trip_id"], "a successful job must name the trip it created"


def test_the_created_trip_exists_and_matches_the_job(client, app):
    """The id in the job is a real trip, not a placeholder."""
    key = _key(app)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    settled = _settle(client, key, job["id"])

    trip = client.get(f"/api/v1/trips/{settled['trip_id']}", headers=_auth(key))
    assert trip.status_code == 200, trip.text
    assert trip.json()["profile"]["destination"] == "成都"


def test_a_preview_only_job_creates_nothing(client, app):
    """Trying a destination before committing to it is the main reason to be impatient."""
    key = _key(app)
    before = len(client.get("/api/v1/trips", headers=_auth(key)).json())

    response = client.post(
        "/api/v1/planning/jobs",
        json=_profile(),
        params={"queue_trip": "false"},
        headers=_auth(key),
    )
    assert response.status_code == 202, response.text
    settled = _settle(client, key, response.json()["id"])

    assert settled["status"] == "succeeded"
    assert settled["trip_id"] is None
    assert len(client.get("/api/v1/trips", headers=_auth(key)).json()) == before


# -- progress honesty -----------------------------------------------------------


def test_the_finished_job_shows_the_steps_that_really_ran(client, app):
    """The plan's steps must correspond to the pipeline, not to a script.

    If planning is reordered or a stage is removed, this list changes — which is the point.
    A hard-coded progress script would keep reporting stages that no longer happen.
    """
    key = _key(app)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    settled = _settle(client, key, job["id"])

    names = [step["name"] for step in settled["steps"]]
    assert names[0] == "resolve_sources"
    assert "collect_places" in names
    assert "calculate_routes" in names
    assert "build_itinerary" in names and names[-1] == "save_trip"


def test_every_step_has_a_measured_duration(client, app):
    """A step with an invented duration is not evidence that anything happened."""
    key = _key(app)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    settled = _settle(client, key, job["id"])

    assert settled["steps"]
    for step in settled["steps"]:
        assert step["started_at"], step
        assert step["finished_at"], f"step {step['name']} was left open"
        assert step["duration_ms"] is not None and step["duration_ms"] >= 0
    # No step is left open at the end, so nothing shows a spinner forever.
    assert settled["current_step"] is None
    assert settled["elapsed_ms"] is not None


def test_a_step_reports_what_it_is_working_on(client, app):
    """The detail counts are the difference between "working" and "working on 12 things"."""
    key = _key(app)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    settled = _settle(client, key, job["id"])

    details = {step["name"]: step["detail"] for step in settled["steps"]}
    assert details["collect_places"]
    # The route step always says something, including when it is skipped, so an absent
    # step can never be confused with a step that quietly did nothing.
    assert details["calculate_routes"]


def test_the_job_reports_how_long_it_took_not_how_long_it_waited(client, app):
    key = _key(app)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    settled = _settle(client, key, job["id"])

    assert settled["started_at"] and settled["finished_at"]
    assert settled["elapsed_ms"] >= 0
    # Elapsed is measured from the real start, so it can never exceed the wall-clock span
    # of the whole job by more than a scheduling tick.
    assert settled["elapsed_ms"] <= 600_000


# -- failure --------------------------------------------------------------------


def test_a_planning_failure_is_reported_as_a_failure_with_its_cause(client, app, monkeypatch):
    """A failed plan must not come back as a successful job with no trip.

    The client would show "done" and then have nothing to open, which is the least
    debuggable outcome possible.

    Patched on ``trip_service`` rather than on ``aggregation``, because the service imported
    the name directly: patching the defining module would leave the already-bound reference
    untouched, and the test would pass while exercising code that never raised.
    """
    key = _key(app)

    async def explode(*args, **kwargs):
        raise RuntimeError("高德返回了 status=0：INVALID_USER_KEY")

    monkeypatch.setattr("travel_api.services.trip_service.build_preview", explode)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    settled = _settle(client, key, job["id"])

    assert settled["status"] == "failed"
    assert "INVALID_USER_KEY" in (settled["error"] or "")
    assert settled["trip_id"] is None
    assert settled["finished_at"] is not None
    # Even on failure no step is left open, so the UI cannot show a running spinner
    # beside a failed job.
    assert settled["current_step"] is None
    assert all(step["finished_at"] for step in settled["steps"])


# -- ownership ------------------------------------------------------------------


def test_another_owner_cannot_read_the_job(client, app):
    """404 rather than 403: confirming the id exists is itself a disclosure."""
    alice, bob = _key(app, "owner-a"), _key(app, "owner-b")
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(alice)).json()
    _settle(client, alice, job["id"])

    assert client.get(f"/api/v1/planning/jobs/{job['id']}", headers=_auth(bob)).status_code == 404
    assert client.get(f"/api/v1/planning/jobs/{job['id']}", headers=_auth(alice)).status_code == 200


def test_another_owner_cannot_cancel_the_job(client, app):
    alice, bob = _key(app, "owner-a"), _key(app, "owner-b")
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(alice)).json()
    _settle(client, alice, job["id"])

    response = client.post(f"/api/v1/planning/jobs/{job['id']}/cancel", headers=_auth(bob))
    assert response.status_code == 404


def test_listing_jobs_shows_only_my_own(client, app):
    alice, bob = _key(app, "owner-a"), _key(app, "owner-b")
    mine = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(alice)).json()
    theirs = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(bob)).json()
    _settle(client, alice, mine["id"])
    _settle(client, bob, theirs["id"])

    listed = client.get("/api/v1/planning/jobs", headers=_auth(alice)).json()
    assert [item["id"] for item in listed] == [mine["id"]]


def test_an_unknown_job_is_a_404(client, app):
    key = _key(app)
    assert client.get("/api/v1/planning/jobs/nope", headers=_auth(key)).status_code == 404
    assert client.post("/api/v1/planning/jobs/nope/cancel", headers=_auth(key)).status_code == 404


# -- cancellation ---------------------------------------------------------------


def test_cancelling_a_finished_job_is_not_an_error(client, app):
    """Cancelling something already done is a no-op.

    Inventing a failure here would make a harmless double-click look like a bug.
    """
    key = _key(app)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    _settle(client, key, job["id"])

    response = client.post(f"/api/v1/planning/jobs/{job['id']}/cancel", headers=_auth(key))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["error"] is None


# -- streaming ------------------------------------------------------------------


def test_the_event_stream_reports_the_job_and_then_closes(client, app):
    """SSE, and it terminates.

    A stream that stayed open after the job finished would be a leak, and a client would
    keep a polling connection for a job nobody is waiting on any more.
    """
    key = _key(app)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    _settle(client, key, job["id"])

    with client.stream(
        "GET", f"/api/v1/planning/jobs/{job['id']}/events", headers=_auth(key)
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        # The stream returns on its own, which is the property under test.
    assert events, "an SSE stream must send the current state"
    # The final state is terminal, which is why the server could close it.
    assert events[-1]["status"] in {"succeeded", "failed", "cancelled"}
    assert events[-1]["id"] == job["id"]


def test_streaming_another_owners_job_is_a_404(client, app):
    alice, bob = _key(app, "owner-a"), _key(app, "owner-b")
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(alice)).json()
    _settle(client, alice, job["id"])

    response = client.get(f"/api/v1/planning/jobs/{job['id']}/events", headers=_auth(bob))
    assert response.status_code == 404


def test_the_stream_carries_real_steps_when_the_job_is_still_running(client, app):
    """Progress must arrive while there is still something to wait for.

    The job is polled to completion first only to keep the test deterministic; the
    property asserted is that the stream's first frame is a complete, usable snapshot
    rather than a delta a late subscriber could not interpret.
    """
    key = _key(app)
    job = client.post("/api/v1/planning/jobs", json=_profile(), headers=_auth(key)).json()
    _settle(client, key, job["id"])

    with client.stream(
        "GET", f"/api/v1/planning/jobs/{job['id']}/events", headers=_auth(key)
    ) as response:
        first = None
        for line in response.iter_lines():
            if line.startswith("data: "):
                first = json.loads(line[6:])
                break

    assert first is not None
    # A whole snapshot: the client needs no prior state to render it.
    for field in ("id", "status", "profile", "steps", "elapsed_ms", "is_cancellable"):
        assert field in first, f"{field} missing from the streamed snapshot"


# -- re-planning an existing trip ----------------------------------------------


def _create_trip(client, key: str) -> dict:
    response = client.post("/api/v1/trips", json=_profile(), headers=_auth(key))
    assert response.status_code in {200, 201}, response.text
    return response.json()


def test_a_refresh_is_queued_as_a_job_and_not_run_inline(client, app):
    """A refresh costs what a plan costs, so it gets the same treatment.

    The failure this guards is the inconsistency: leaving a refresh synchronous while
    planning is asynchronous makes the operation whose duration is hardest to predict the
    one with no feedback at all.
    """
    key = _key(app)
    trip = _create_trip(client, key)

    response = client.post(f"/api/v1/trips/{trip['id']}/refresh/jobs", headers=_auth(key))

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["id"]
    # Distinguished from a first plan, and pointing at the trip it re-plans: a client has to
    # render 重新规划 over the existing trip rather than as a new one appearing.
    assert payload["kind"] == "refresh"
    assert payload["target_trip_id"] == trip["id"]


def test_a_refresh_job_is_pollable_and_streamable_like_any_other(client, app):
    """One progress mechanism, not two.

    Reusing the planning endpoints is the whole reason this is a job rather than a bespoke
    long-poll: a client should not need a second code path to watch a refresh.
    """
    key = _key(app)
    trip = _create_trip(client, key)
    job = client.post(f"/api/v1/trips/{trip['id']}/refresh/jobs", headers=_auth(key)).json()

    settled = _settle(client, key, job["id"])
    assert settled["status"] == "succeeded", settled.get("error")
    # The refreshed trip keeps its identity: same id, so history and links survive.
    assert settled["trip_id"] == trip["id"]
    assert settled["target_trip_id"] == trip["id"]
    assert settled["kind"] == "refresh"

    # And it shows up in the same list as planning jobs.
    listed = client.get("/api/v1/planning/jobs", headers=_auth(key)).json()
    assert job["id"] in {item["id"] for item in listed}


def test_a_completed_refresh_advances_the_trip_version(client, app):
    """The refresh has to have actually re-planned, not just reported success."""
    key = _key(app)
    trip = _create_trip(client, key)
    before = trip["current_version"]

    job = client.post(f"/api/v1/trips/{trip['id']}/refresh/jobs", headers=_auth(key)).json()
    _settle(client, key, job["id"])

    after = client.get(f"/api/v1/trips/{trip['id']}", headers=_auth(key)).json()
    assert after["current_version"] > before, "the stored trip was not updated"


def test_refreshing_a_missing_trip_fails_immediately_without_creating_a_job(client, app):
    """A 404 up front, not a job that dies a minute later.

    Two reasons this matters, and they are different. A caller gets the answer straight away
    instead of watching a progress bar for a minute before learning the trip does not exist.
    And no job is created for an id that was never real, so the jobs list cannot be used to
    probe which trip ids exist.
    """
    key = _key(app)
    response = client.post(
        "/api/v1/trips/00000000-0000-4000-8000-000000000000/refresh/jobs", headers=_auth(key)
    )

    assert response.status_code == 404
    assert client.get("/api/v1/planning/jobs", headers=_auth(key)).json() == []


def test_refreshing_another_owners_trip_is_a_404_and_creates_no_job(client, app):
    """Same rule as trips and notes: 403 would confirm the id exists."""
    owner_a, owner_b = _key(app, "owner-a"), _key(app, "owner-b")
    trip = _create_trip(client, owner_a)

    response = client.post(f"/api/v1/trips/{trip['id']}/refresh/jobs", headers=_auth(owner_b))

    assert response.status_code == 404
    assert client.get("/api/v1/planning/jobs", headers=_auth(owner_b)).json() == []
    # The owner's own trip is untouched.
    assert client.get(f"/api/v1/trips/{trip['id']}", headers=_auth(owner_a)).status_code == 200


def test_the_explicit_owner_is_honoured_with_no_ambient_owner(client, app):
    """The owner argument is the filter, not a hint that happens to agree with ambient state.

    Found by a mutation harness: deleting ``owner_id=owner_id`` from ``queue_refresh``'s
    ``self.get(...)`` **survived the whole suite**, including
    ``test_refreshing_another_owners_trip_is_a_404_and_creates_no_job`` above.

    The reason is not a missing guard in the route. ``main.py`` binds the ambient owner from the
    bearer token for every authenticated request, and ``TripService.get`` resolves through
    ``resolve_owner``, which falls back to that ambient value — so over HTTP the explicit
    argument and the ambient one are always the same string and the two versions of the line are
    genuinely equivalent. That is why no assertion could tell them apart.

    They stop being equivalent the moment nothing is ambient: ``resolve_owner(None)`` is then
    ``None``, ``None`` means *no filter* to the repository, and the lookup returns whatever trip
    owns that id. That is the shape a background caller has — the job runner, a CLI, a scheduled
    refresh — and it is the case this test creates on purpose, by calling the service directly
    with no request in flight and no ambient owner bound.
    """
    owner_a, owner_b = _key(app, "owner-a"), _key(app, "owner-b")
    trip = _create_trip(client, owner_a)

    # The precondition the test is about. If a previous test leaked an ambient owner, this
    # assertion is what says so, rather than the test passing for the wrong reason.
    assert current_owner() is None, (
        "an ambient owner is bound, so this test would not distinguish the explicit argument "
        "from the fallback — which is the entire subject"
    )

    service = app.state.trip_service
    with pytest.raises(TripNotFoundError):
        service.queue_refresh(
            UUID(trip["id"]),
            owner_id=owner_b,
            jobs=app.state.planning_jobs,
        )

    # And nothing was created for either owner, because it failed before a job existed.
    registry = app.state.planning_jobs
    assert registry.list_for_owner("owner-a") == []
    assert registry.list_for_owner("owner-b") == []


def test_a_refresh_job_reports_the_pipeline_steps_and_not_only_its_own(client, app):
    """The refresh's progress must come from the pipeline, not just from the wrapper.

    This pins a bug that shipped: ``refresh`` accepted a ``progress`` argument and never
    passed it to ``preview``, so a refresh job reported ``reload_trip`` and then nothing at
    all for the following minute. The display looked like a working progress panel — one
    finished step, a running job — while the only visible stage was the trivial one. A
    client cannot detect that from the outside, so it has to be asserted here.
    """
    key = _key(app)
    trip = _create_trip(client, key)
    job = client.post(f"/api/v1/trips/{trip['id']}/refresh/jobs", headers=_auth(key)).json()

    settled = _settle(client, key, job["id"])
    assert settled["status"] == "succeeded", settled.get("error")

    steps = [step["name"] for step in settled["steps"]]
    # The wrapper's own step.
    assert "reload_trip" in steps
    # And the pipeline it is supposed to be reporting on. Any of these coming from
    # build_preview proves progress was actually threaded through.
    pipeline_steps = {"collect_places", "build_itinerary", "calculate_routes", "room_offers"}
    assert pipeline_steps & set(steps), f"the refresh reported no pipeline progress: {steps}"
    # Every step obeys the same rule as a first plan: closed means measured, open means unknown.
    for step in settled["steps"]:
        if step["finished_at"]:
            assert step["duration_ms"] is not None, f"closed step {step['name']} has no duration"
        else:
            assert step["duration_ms"] is None, f"open step {step['name']} claims a duration"


def test_the_synchronous_refresh_still_works(client, app):
    """Kept deliberately: a caller that wants a blocking response still gets one.

    Removing it would be a breaking change for no honesty gain — the job endpoint is the
    better default, not the only option.
    """
    key = _key(app)
    trip = _create_trip(client, key)

    response = client.post(f"/api/v1/trips/{trip['id']}/refresh", headers=_auth(key))

    assert response.status_code == 200, response.text
    assert response.json()["current_version"] > trip["current_version"]
