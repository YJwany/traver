"""Whose credentials apply to the work currently in flight.

Why this exists
---------------
Sources became per-owner: 高德's key belongs to one person, the free sources are
shared. ``TripService.preview`` therefore needs an ``owner_id``, and the obvious
design is to pass it down as a parameter.

That works for the REST path, where the owner is in hand. It does not work for the
**AI tool path**: ``ToolOrchestrator`` is constructed once, holds the tool
registry, and invokes handlers that call ``trip_service.preview(...)``. The owner
is known at the top (``TravelAIService.chat(request, owner_id=...)``) but the
orchestrator and every handler signature in between would have to carry it purely
to forward it.

The alternative considered was threading it anyway. It was rejected because the
handler signatures are a **published contract**: they are the OpenAI tool
definitions the model sees. Adding an ``owner_id`` parameter to them would either
leak an internal concern into the model-facing schema or require a parallel
out-of-band channel that is harder to test than this one.

So the owner is ambient for the duration of one request, via ``ContextVar``:

* set once at the boundary (the request dependency, or ``chat``);
* read by ``TripService`` only when no explicit owner was passed;
* always reset in a ``finally``, and task-scoped, so concurrent requests cannot
  observe each other's value — ``asyncio.gather`` copies the context into each
  child task.

An explicit argument always wins, so this is a fallback rather than a second
source of truth.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

#: ``None`` means "not set". An empty string is a real owner id in
#: ``AuthService`` (the anonymous owner), so the two must stay distinguishable and
#: ``None`` is used for absence.
_current_owner: ContextVar[str | None] = ContextVar("traver_current_owner", default=None)


def current_owner() -> str | None:
    """The owner acting on the current task, or ``None`` if nobody set one."""
    return _current_owner.get()


def set_current_owner(owner_id: str | None) -> Token:
    """Set the acting owner; returns a token for :func:`reset_current_owner`."""
    return _current_owner.set(owner_id)


def reset_current_owner(token: Token) -> None:
    _current_owner.reset(token)


@contextmanager
def acting_as(owner_id: str | None) -> Iterator[None]:
    """Run a block as ``owner_id``, restoring the previous value afterwards.

    The reset is in a ``finally`` so an exception mid-planning cannot leave the
    owner set for whatever this task does next.
    """
    token = set_current_owner(owner_id)
    try:
        yield
    finally:
        reset_current_owner(token)


def resolve_owner(explicit: str | None) -> str | None:
    """An explicit owner if given, otherwise the ambient one.

    ``explicit`` wins even when it is falsy-but-not-``None``: a caller that
    deliberately passed ``""`` (the anonymous owner) must not be silently upgraded
    to whoever happens to be ambient.
    """
    return explicit if explicit is not None else current_owner()
