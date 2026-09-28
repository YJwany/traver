"""Long-running plan generation, with progress a user can watch.

Planning is not instant and cannot honestly be made to look instant. A single Overpass
query was measured at 36.5 seconds, and a real plan fans out across several sources before
it can even start computing routes. A synchronous request that blocks for that long is not
just slow, it looks broken: the browser spins, nothing explains why, and the traveller has
no way to tell "working" from "hung".

So planning is queued as a job, and the job reports what it is actually doing.

Two design commitments, both about not lying to the user:

**The steps are real.** Each one is emitted at a boundary the code genuinely crosses, and
each carries the wall-clock times it started and finished. A progress bar driven by a
timer is worse than no progress bar: it advances smoothly while nothing happens and then
jumps, which teaches the user to distrust it. Here, if nothing is moving, the timestamps
say so.

**A job's outcome is one of five states, and failure is not masked.** A plan that fails
reports the real error rather than an empty trip. A cancelled job says it was cancelled
rather than reporting success with no data.

The registry is in-process, which is a real limitation and is stated rather than hidden: a
deployment running several worker processes would need a shared store before a job created
on one worker could be polled on another. For the single-process deployment this targets,
an in-process registry is honest and has no external dependency to fail.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from travel_api.domain.models import TripProfile

logger = logging.getLogger(__name__)


class JobStatus(StrEnum):
    """Where a planning job is.

    ``CANCELLED`` is deliberately not folded into ``FAILED``: one is the user's decision
    and the other is a problem, and reporting a cancelled job as a failure would send the
    user looking for a bug that does not exist.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


class JobStep(BaseModel):
    """One boundary the planner actually crossed.

    ``started_at`` and ``finished_at`` are real clock readings, so a stalled step is
    visible as a long-running one rather than hidden behind an animation. A step with no
    ``finished_at`` is genuinely still in progress.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    detail: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


class PlanningJob(BaseModel):
    """A queued or completed plan, with its history.

    ``profile`` is kept so a client that only stored the job id can still render what the
    plan is for; it contains nothing beyond what the requester supplied.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    owner_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    profile: TripProfile
    budget_strategy: str = "balanced"
    steps: list[JobStep] = Field(default_factory=list)
    #: The created trip, once there is one. Absent while running, and absent when the job
    #: failed — never a placeholder, because a placeholder id would look like a trip.
    trip_id: UUID | None = None
    #: The real failure, verbatim. A generic "planning failed" would hide the one detail
    #: that makes the problem fixable.
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Set when cancellation was requested. The worker checks it between steps, so a
    #: cancel takes effect at the next boundary instead of tearing down a call in flight.
    cancel_requested: bool = False

    @property
    def elapsed_ms(self) -> int | None:
        """Total wall-clock time so far, from the start that actually happened.

        Measured from ``started_at`` rather than ``created_at`` so queue wait is not
        reported as work.
        """
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return int((end - self.started_at).total_seconds() * 1000)

    @property
    def current_step(self) -> str | None:
        for step in reversed(self.steps):
            if step.finished_at is None:
                return step.name
        return None

    @property
    def is_cancellable(self) -> bool:
        return not self.status.is_terminal and not self.cancel_requested


#: What a runner is given: the job (to report against) and the owner to run as.
JobRunner = Callable[[PlanningJob], Awaitable[UUID | None]]


class PlanningJobRegistry:
    """In-process job store with a bounded history.

    Bounded because an unbounded map of finished jobs is a memory leak with a friendly
    name. Oldest finished jobs are evicted first, so work in progress is never dropped.
    """

    def __init__(self, *, max_jobs: int = 200) -> None:
        self._jobs: OrderedDict[str, PlanningJob] = OrderedDict()
        self._tasks: dict[str, asyncio.Task] = {}
        self._listeners: dict[str, list[asyncio.Queue]] = {}
        self._max_jobs = max_jobs

    # -- lifecycle ----------------------------------------------------------

    def submit(
        self,
        profile: TripProfile,
        runner: JobRunner,
        *,
        owner_id: str | None = None,
        budget_strategy: str = "balanced",
    ) -> PlanningJob:
        """Queue a job and start it on the current event loop.

        Returns immediately. The caller gets an id to poll or stream; the work happens in
        the background, which is the entire point.
        """
        job = PlanningJob(profile=profile, owner_id=owner_id, budget_strategy=budget_strategy)
        self._jobs[job.id] = job
        self._evict_if_needed()
        task = asyncio.create_task(self._run(job, runner))
        self._tasks[job.id] = task
        # The task holds its own reference; this only cleans the bookkeeping up and makes
        # sure an exception can never be an unretrieved one.
        task.add_done_callback(lambda finished: self._forget(finished, job.id))
        return job

    async def _run(self, job: PlanningJob, runner: JobRunner) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        await self._publish(job)
        try:
            trip_id = await runner(job)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            await self._finish(job)
            raise
        except Exception as exc:  # noqa: BLE001 - the message is the payload
            logger.warning("planning job %s failed: %s", job.id, exc)
            job.status = JobStatus.FAILED
            # The real message, not a generic one: it is the only thing that makes the
            # failure actionable, and this is a local planning tool, not a public API
            # where internals need hiding.
            job.error = str(exc) or exc.__class__.__name__
        else:
            if job.cancel_requested:
                # Cancellation arrives between steps, so a runner may finish normally
                # after a cancel was asked for. Reporting success would contradict what
                # the user was told.
                job.status = JobStatus.CANCELLED
            else:
                job.status = JobStatus.SUCCEEDED
                job.trip_id = trip_id
        finally:
            await self._finish(job)

    async def _finish(self, job: PlanningJob) -> None:
        if job.finished_at is None:
            job.finished_at = datetime.now(timezone.utc)
        # Close any step still open, so a finished job never shows a spinner forever.
        for step in job.steps:
            if step.finished_at is None:
                step.finished_at = job.finished_at
        await self._publish(job)

    def _forget(self, task: asyncio.Task, job_id: str) -> None:
        self._tasks.pop(job_id, None)
        if task.cancelled():
            return
        # Retrieving the exception stops asyncio from logging "never retrieved"; the
        # status recorded on the job is what callers see.
        task.exception()

    def _evict_if_needed(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        for job_id, job in list(self._jobs.items()):
            if job.status.is_terminal:
                del self._jobs[job_id]
                self._listeners.pop(job_id, None)
                if len(self._jobs) <= self._max_jobs:
                    break

    # -- reading ------------------------------------------------------------

    def get(self, job_id: str, *, owner_id: str | None = None) -> PlanningJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        # Same rule as stored notes: a job carries the requester's trip parameters, so
        # another owner must not be able to read it, and a miss is reported identically
        # whether the id is unknown or simply not theirs.
        if owner_id is not None and job.owner_id != owner_id:
            return None
        return job

    def list_for_owner(self, owner_id: str | None = None) -> list[PlanningJob]:
        return [job for job in self._jobs.values() if owner_id is None or job.owner_id == owner_id]

    def cancel(self, job_id: str, *, owner_id: str | None = None) -> PlanningJob | None:
        """Ask a job to stop, and say whether the request landed.

        Returns the job so the caller can see the resulting status rather than assuming
        it. A job that has already finished is not an error — cancelling something that
        is done is a no-op, and pretending otherwise would invent a failure.
        """
        job = self.get(job_id, owner_id=owner_id)
        if job is None:
            return None
        if job.status.is_terminal:
            return job
        job.cancel_requested = True
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return job

    # -- progress -----------------------------------------------------------

    def subscribe(self, job_id: str) -> asyncio.Queue:
        """A queue of job snapshots, for a streaming endpoint to drain."""
        queue: asyncio.Queue = asyncio.Queue()
        self._listeners.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        listeners = self._listeners.get(job_id)
        if not listeners:
            return
        if queue in listeners:
            listeners.remove(queue)
        if not listeners:
            self._listeners.pop(job_id, None)

    async def _publish(self, job: PlanningJob) -> None:
        """Hand the current snapshot to every listener.

        Snapshots rather than deltas: a client that connects late, or reconnects, gets the
        whole state and cannot end up with a progress display that disagrees with the
        server. The payload is small enough that this costs nothing.
        """
        for queue in list(self._listeners.get(job.id, [])):
            # Never block on a slow reader: a stalled stream must not stall planning.
            try:
                queue.put_nowait(job.model_copy(deep=True))
            except asyncio.QueueFull:  # pragma: no cover - unbounded queues
                logger.warning("dropping progress update for job %s", job.id)

    # -- progress reporting for runners -------------------------------------

    async def step(self, job: PlanningJob, name: str, detail: str = "") -> None:
        """Open a step and publish it immediately.

        Published on open, not on close, so a slow step is visible *while* it is slow —
        which is the only time progress information is worth anything.
        """
        for previous in job.steps:
            if previous.finished_at is None:
                previous.finished_at = datetime.now(timezone.utc)
        job.steps.append(JobStep(name=name, detail=detail))
        await self._publish(job)

    async def finish_step(self, job: PlanningJob) -> None:
        for step in reversed(job.steps):
            if step.finished_at is None:
                step.finished_at = datetime.now(timezone.utc)
                break
        await self._publish(job)

    def check_cancelled(self, job: PlanningJob) -> None:
        """Raise if cancellation was requested, for runners to call between stages.

        An explicit check rather than relying on ``Task.cancel`` alone: cancellation has to
        be honoured at a boundary where partial work can be abandoned cleanly, not in the
        middle of writing a trip.
        """
        if job.cancel_requested:
            raise asyncio.CancelledError()

    def progress(self, job: PlanningJob) -> JobProgress:
        """A reporter for one job, for handing to the planner."""
        return JobProgress(self, job)


class JobProgress:
    """A job bound to its registry, in the shape the planner reports through.

    The planner must not know that jobs exist — the synchronous endpoint passes nothing and
    behaves identically — so it reports through a two-method interface (``step`` /
    ``finish_step``). The registry's own methods take the job explicitly, because one
    registry serves many jobs. This adapter is the single place those two shapes meet,
    which is why the planner never has to thread a job object through its call chain.
    """

    __slots__ = ("_registry", "_job")

    def __init__(self, registry: PlanningJobRegistry, job: PlanningJob) -> None:
        self._registry = registry
        self._job = job

    async def step(self, name: str, detail: str = "") -> None:
        await self._registry.step(self._job, name, detail)

    async def finish_step(self) -> None:
        await self._registry.finish_step(self._job)

    def check_cancelled(self) -> None:
        self._registry.check_cancelled(self._job)


def job_as_dict(job: PlanningJob) -> dict[str, Any]:
    """Serialise a job for the API, computing the derived values a client needs."""
    return {
        "id": job.id,
        "status": str(job.status),
        "profile": job.profile.model_dump(mode="json"),
        "budget_strategy": job.budget_strategy,
        "steps": [
            {
                "name": step.name,
                "detail": step.detail,
                "started_at": step.started_at.isoformat(),
                "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                "duration_ms": step.duration_ms,
            }
            for step in job.steps
        ],
        "current_step": job.current_step,
        "trip_id": str(job.trip_id) if job.trip_id else None,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "elapsed_ms": job.elapsed_ms,
        "cancel_requested": job.cancel_requested,
        "is_cancellable": job.is_cancellable,
    }
