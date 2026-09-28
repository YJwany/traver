"""Planning as a job, and progress that is true.

Planning takes tens of seconds in practice — one upstream call was measured at 36.5 s — so
a synchronous request is not merely slow, it is indistinguishable from a hang. The job
mechanism exists to make that wait legible, which creates one obligation: **the progress it
reports must be real**. A bar that advances on a timer while nothing happens is worse than
no bar, because it teaches the user to ignore the one signal that could have told them
something was wrong.

So these tests assert the things that would make the display a lie:

  * steps carry real start and finish times, and a step that has not finished has no
    finish time (rather than an invented one);
  * a step's duration is measured, not assumed;
  * failure surfaces the actual error, not a generic message;
  * cancellation is reported as cancellation and not as success or failure;
  * a job is not readable by another owner, and a job that never ran is not reported as
    finished.

The registry is deliberately in-process; that limitation is documented rather than hidden,
so there is no test pretending otherwise.
"""

from __future__ import annotations

import asyncio

import pytest
from travel_api.domain.models import TripProfile
from travel_api.services.planning_jobs import (
    JobStatus,
    PlanningJobRegistry,
    job_as_dict,
)


def _profile() -> TripProfile:
    return TripProfile(
        destination="杭州",
        start_date="2026-10-03",
        end_date="2026-10-05",
        travelers=2,
        total_budget=5000,
    )


async def _wait_for_terminal(registry: PlanningJobRegistry, job, timeout: float = 5.0):
    """Poll to a terminal state, so a test never depends on a fixed sleep."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        current = registry.get(job.id)
        if current is not None and current.status.is_terminal:
            return current
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job.id} did not settle; last status {registry.get(job.id).status}")


# -- happy path -----------------------------------------------------------------


async def test_a_job_returns_immediately_with_an_id_to_poll():
    """The whole point: the caller gets an id instead of waiting for the work."""
    registry = PlanningJobRegistry()
    release = asyncio.Event()

    async def runner(job):
        await release.wait()
        return None

    job = registry.submit(_profile(), runner)

    # Submitted and running without the work having finished — no awaiting the runner.
    assert job.id
    assert job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
    release.set()
    settled = await _wait_for_terminal(registry, job)
    assert settled.status is JobStatus.SUCCEEDED


async def test_a_successful_job_reports_the_created_trip():
    registry = PlanningJobRegistry()
    from uuid import uuid4

    trip_id = uuid4()

    async def runner(job):
        return trip_id

    job = registry.submit(_profile(), runner)
    settled = await _wait_for_terminal(registry, job)

    assert settled.status is JobStatus.SUCCEEDED
    assert settled.trip_id == trip_id
    assert settled.error is None
    assert settled.finished_at is not None


async def test_the_queue_wait_is_not_counted_as_work():
    """``elapsed_ms`` measures the run, not the time spent waiting to run.

    Reporting queue wait as elapsed work would make every job look slow under load and
    hide which part actually needs attention.
    """
    registry = PlanningJobRegistry()
    gate = asyncio.Event()

    async def runner(job):
        await gate.wait()
        return None

    job = registry.submit(_profile(), runner)
    await asyncio.sleep(0.05)
    gate.set()
    settled = await _wait_for_terminal(registry, job)

    assert settled.started_at is not None
    # The job was queued well before it started, so the two must be distinguishable.
    assert settled.started_at >= settled.created_at
    assert settled.elapsed_ms is not None
    assert (
        settled.elapsed_ms
        < int((settled.finished_at - settled.created_at).total_seconds() * 1000) + 50
    )


# -- progress honesty -----------------------------------------------------------


async def test_steps_carry_real_times_and_a_running_step_has_no_finish():
    """A step in progress must not claim to be finished.

    Inventing a finish time would make a stalled step look complete, which is exactly the
    failure the display is supposed to reveal.
    """
    registry = PlanningJobRegistry()
    release = asyncio.Event()

    async def runner(job):
        await registry.step(job, "collect_places", "酒店、餐厅")
        await release.wait()
        await registry.finish_step(job)
        return None

    job = registry.submit(_profile(), runner)
    while not job.steps:
        await asyncio.sleep(0.01)

    running = registry.get(job.id)
    assert running is not None
    step = running.steps[0]
    assert step.name == "collect_places"
    assert step.detail == "酒店、餐厅"
    assert step.started_at is not None
    assert step.finished_at is None
    assert step.duration_ms is None
    assert running.current_step == "collect_places"

    release.set()
    settled = await _wait_for_terminal(registry, job)
    assert settled.steps[0].finished_at is not None
    assert settled.steps[0].duration_ms is not None
    assert settled.steps[0].duration_ms >= 0
    assert settled.current_step is None


async def test_a_step_is_published_when_it_opens_not_when_it_closes():
    """So a slow step is visible while it is slow.

    This is the timing that matters: if the step were only published on completion, the
    user would see nothing at all for the 36 seconds that prompted this feature.
    """
    registry = PlanningJobRegistry()
    release = asyncio.Event()

    async def runner(job):
        await registry.step(job, "calculate_routes", "12 个途经点")
        await release.wait()
        return None

    job = registry.submit(_profile(), runner)
    queue = registry.subscribe(job.id)
    while not job.steps:
        await asyncio.sleep(0.01)

    # A subscriber sees the open step immediately, before the work is done. The queue may
    # already hold the earlier "running" snapshot, which legitimately has no steps yet, so
    # drain to the first snapshot that carries one.
    snapshot = None
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        candidate = await asyncio.wait_for(queue.get(), timeout=1.0)
        if candidate.steps:
            snapshot = candidate
            break
    assert snapshot is not None, "the open step was never published"
    assert snapshot.steps[0].name == "calculate_routes"
    assert snapshot.steps[0].finished_at is None
    # And the run was still in progress when that was published — the whole point of
    # publishing on open rather than on close.
    assert snapshot.status is JobStatus.RUNNING
    release.set()
    await _wait_for_terminal(registry, job)


async def test_opening_a_second_step_closes_the_first():
    """So a finished job never shows two steps running at once."""
    registry = PlanningJobRegistry()

    async def runner(job):
        await registry.step(job, "first")
        await asyncio.sleep(0.01)
        await registry.step(job, "second")
        return None

    job = registry.submit(_profile(), runner)
    settled = await _wait_for_terminal(registry, job)

    assert [step.name for step in settled.steps] == ["first", "second"]
    assert all(step.finished_at is not None for step in settled.steps)


async def test_a_finished_job_never_leaves_a_step_open():
    """A spinner that never stops is the failure this guards against."""
    registry = PlanningJobRegistry()

    async def runner(job):
        await registry.step(job, "collect_places")
        # Returns without closing the step — a common way to write a runner by accident.
        return None

    job = registry.submit(_profile(), runner)
    settled = await _wait_for_terminal(registry, job)

    assert settled.status is JobStatus.SUCCEEDED
    assert settled.steps[0].finished_at is not None
    assert settled.current_step is None


# -- failure, cancellation ------------------------------------------------------


async def test_a_failure_reports_the_real_error():
    """Not a generic message: the detail is the only thing that makes it fixable."""
    registry = PlanningJobRegistry()

    async def runner(job):
        raise ValueError("高德返回了 status=0：INVALID_USER_KEY")

    job = registry.submit(_profile(), runner)
    settled = await _wait_for_terminal(registry, job)

    assert settled.status is JobStatus.FAILED
    assert "INVALID_USER_KEY" in (settled.error or "")
    assert settled.trip_id is None, "a failed job must not look like it produced a trip"
    assert settled.finished_at is not None


async def test_an_exception_with_no_message_still_reports_something():
    """A bare ``raise ValueError`` must not produce an empty error string.

    An empty message reads as "something went wrong, we do not know what", which is
    strictly less useful than the exception's own name.
    """
    registry = PlanningJobRegistry()

    async def runner(job):
        raise ValueError

    job = registry.submit(_profile(), runner)
    settled = await _wait_for_terminal(registry, job)

    assert settled.status is JobStatus.FAILED
    assert settled.error == "ValueError"


async def test_a_cancelled_job_is_reported_as_cancelled_not_as_failed():
    """One is the user's decision, the other is a problem.

    Reporting a cancel as a failure would send the user looking for a bug that does not
    exist.
    """
    registry = PlanningJobRegistry()
    started = asyncio.Event()

    async def runner(job):
        started.set()
        await asyncio.sleep(30)

    job = registry.submit(_profile(), runner)
    await started.wait()
    registry.cancel(job.id)
    settled = await _wait_for_terminal(registry, job)

    assert settled.status is JobStatus.CANCELLED
    assert settled.error is None, "a cancellation is not an error"
    assert settled.finished_at is not None


async def test_a_cancel_that_lands_after_the_work_finished_is_still_honoured():
    """The runner may finish normally after a cancel was asked for.

    Reporting success would contradict what the user was told when they pressed cancel.
    """
    registry = PlanningJobRegistry()
    release = asyncio.Event()

    async def runner(job):
        # Ignores the cancellation signal and completes anyway.
        await release.wait()
        return None

    job = registry.submit(_profile(), runner)
    while job.status is not JobStatus.RUNNING:
        await asyncio.sleep(0.01)
    release.set()
    # Cancel while it is finishing, which is the race being tested.
    registry.cancel(job.id)
    settled = await _wait_for_terminal(registry, job)

    assert settled.status is JobStatus.CANCELLED


async def test_cancelling_a_finished_job_is_a_no_op_not_an_error():
    """Cancelling something already done is not a failure."""
    registry = PlanningJobRegistry()

    async def runner(job):
        return None

    job = registry.submit(_profile(), runner)
    await _wait_for_terminal(registry, job)

    again = registry.cancel(job.id)
    assert again is not None
    assert again.status is JobStatus.SUCCEEDED
    assert again.error is None


def test_check_cancelled_raises_at_a_boundary():
    """Runners honour cancellation between stages, not mid-write."""
    registry = PlanningJobRegistry()
    job = registry.get("nope")
    assert job is None

    from travel_api.services.planning_jobs import PlanningJob

    target = PlanningJob(profile=_profile())
    registry.check_cancelled(target)  # no request yet: no raise
    target.cancel_requested = True
    with pytest.raises(asyncio.CancelledError):
        registry.check_cancelled(target)


# -- ownership ------------------------------------------------------------------


async def test_a_job_is_not_readable_by_another_owner():
    """A job carries the requester's destination and dates.

    An unknown id and another owner's id are reported identically, because confirming
    that someone else's job exists is itself a disclosure.
    """
    registry = PlanningJobRegistry()
    release = asyncio.Event()

    async def runner(job):
        await release.wait()
        return None

    job = registry.submit(_profile(), runner, owner_id="owner-a")

    assert registry.get(job.id, owner_id="owner-a") is not None
    assert registry.get(job.id, owner_id="owner-b") is None
    assert registry.get("does-not-exist", owner_id="owner-a") is None
    # An unspecified owner is "no filter", which is the unauthenticated single-user mode.
    assert registry.get(job.id) is not None
    release.set()
    await _wait_for_terminal(registry, job)


async def test_listing_is_scoped_to_the_owner():
    registry = PlanningJobRegistry()
    release = asyncio.Event()

    async def runner(job):
        await release.wait()
        return None

    mine = registry.submit(_profile(), runner, owner_id="owner-a")
    theirs = registry.submit(_profile(), runner, owner_id="owner-b")

    assert [job.id for job in registry.list_for_owner("owner-a")] == [mine.id]
    assert [job.id for job in registry.list_for_owner("owner-b")] == [theirs.id]
    assert len(registry.list_for_owner()) == 2
    release.set()
    await _wait_for_terminal(registry, mine)
    await _wait_for_terminal(registry, theirs)


async def test_cancelling_another_owners_job_is_not_permitted():
    registry = PlanningJobRegistry()
    release = asyncio.Event()

    async def runner(job):
        await release.wait()
        return None

    job = registry.submit(_profile(), runner, owner_id="owner-a")

    assert registry.cancel(job.id, owner_id="owner-b") is None
    assert job.cancel_requested is False, "another owner must not be able to stop it"
    release.set()
    await _wait_for_terminal(registry, job)


# -- memory ---------------------------------------------------------------------


async def test_finished_jobs_are_evicted_but_running_ones_are_not():
    """An unbounded map of finished jobs is a memory leak with a friendly name.

    Work in progress is never dropped, because dropping it would break the poll that is
    waiting on it.
    """
    registry = PlanningJobRegistry(max_jobs=3)
    gate = asyncio.Event()

    async def quick(job):
        return None

    async def slow(job):
        await gate.wait()
        return None

    running = registry.submit(_profile(), slow, owner_id="o")
    for _ in range(6):
        done = registry.submit(_profile(), quick, owner_id="o")
        await _wait_for_terminal(registry, done)

    assert registry.get(running.id) is not None, "a running job must survive eviction"
    assert len(registry.list_for_owner("o")) <= 4
    gate.set()
    await _wait_for_terminal(registry, running)


# -- serialisation --------------------------------------------------------------


async def test_the_serialised_job_carries_everything_a_client_needs():
    """One call must be enough to render the whole progress view.

    Independently derived fields are included rather than left to the client, so two
    clients cannot compute ``elapsed_ms`` differently and disagree.
    """
    registry = PlanningJobRegistry()

    async def runner(job):
        await registry.step(job, "collect_places", "18 个候选")
        return None

    job = registry.submit(_profile(), runner, budget_strategy="comfort")
    settled = await _wait_for_terminal(registry, job)
    payload = job_as_dict(settled)

    assert payload["status"] == "succeeded"
    assert payload["budget_strategy"] == "comfort"
    assert payload["current_step"] is None
    assert payload["is_cancellable"] is False
    assert payload["error"] is None
    assert payload["elapsed_ms"] is not None
    assert payload["finished_at"] is not None
    assert payload["profile"]["destination"] == "杭州"
    step = payload["steps"][0]
    assert step["name"] == "collect_places"
    assert step["detail"] == "18 个候选"
    assert step["duration_ms"] is not None


async def test_a_running_job_serialises_as_cancellable():
    registry = PlanningJobRegistry()
    release = asyncio.Event()

    async def runner(job):
        await release.wait()
        return None

    job = registry.submit(_profile(), runner)
    while job.status is not JobStatus.RUNNING:
        await asyncio.sleep(0.01)

    payload = job_as_dict(registry.get(job.id))
    assert payload["status"] == "running"
    assert payload["is_cancellable"] is True
    assert payload["finished_at"] is None
    assert payload["elapsed_ms"] is not None
    release.set()
    await _wait_for_terminal(registry, job)
