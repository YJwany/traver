"""Tests for the ambient owner context.

``owner_context`` is the one piece of implicit state in the request path, so its
rules are pinned down rather than assumed. The failure modes it protects against are
all cross-request leaks, which are invisible until two owners are active at once.
"""

from __future__ import annotations

import asyncio

import pytest
from travel_api.services.owner_context import (
    acting_as,
    current_owner,
    resolve_owner,
    set_current_owner,
)


@pytest.fixture(autouse=True)
def _clear_owner():
    """Leave no owner bound between tests.

    Written after getting it wrong once: ``reset_current_owner(set_current_owner(None))``
    does NOT clear the value, because the token restores whatever was there before the
    ``set`` — which is the value being replaced. The next test then observed
    ``"outer"`` and failed in a way that looked like a concurrency bug in the
    implementation rather than a leak in the test.
    """
    set_current_owner(None)
    yield
    set_current_owner(None)


def test_nothing_is_set_by_default():
    assert current_owner() is None


def test_an_explicit_owner_wins_over_the_ambient_one():
    with acting_as("ambient"):
        assert resolve_owner("explicit") == "explicit"


def test_the_ambient_owner_is_used_when_no_argument_is_given():
    with acting_as("ambient"):
        assert resolve_owner(None) == "ambient"


def test_an_explicit_empty_string_is_not_upgraded_to_the_ambient_owner():
    """``""`` is the real anonymous owner id, distinct from "not specified".

    Treating it as absent would silently promote an anonymous request to whichever
    owner happened to be ambient — a credential leak in the direction that matters.
    """
    with acting_as("someone-else"):
        assert resolve_owner("") == ""


def test_the_previous_value_is_restored_on_exit():
    with acting_as("outer"):
        with acting_as("inner"):
            assert current_owner() == "inner"
        assert current_owner() == "outer"


def test_the_value_is_restored_even_when_the_block_raises():
    with acting_as("outer"):
        try:
            with acting_as("inner"):
                raise RuntimeError("planning failed")
        except RuntimeError:
            pass
        # Without the `finally`, a failed request would leave its owner bound for
        # whatever this task did next.
        assert current_owner() == "outer"


def test_contexts_are_isolated_between_concurrent_tasks():
    """The reason a ContextVar is safe here and a module global would not be.

    ``asyncio.gather`` copies the context into each child task, so two requests
    interleaved on one event loop cannot observe each other's owner.
    """

    async def worker(owner: str, delay: float) -> list[str | None]:
        seen: list[str | None] = []
        with acting_as(owner):
            seen.append(current_owner())
            await asyncio.sleep(delay)
            # Still this task's owner after yielding, even though the other task
            # ran in between.
            seen.append(current_owner())
        return seen

    async def main() -> tuple[list, list]:
        return await asyncio.gather(worker("owner-a", 0.01), worker("owner-b", 0.0))

    first, second = asyncio.run(main())
    assert first == ["owner-a", "owner-a"]
    assert second == ["owner-b", "owner-b"]
    # And the outer context is untouched afterwards.
    assert current_owner() is None


def test_nesting_lets_an_inner_scope_override_and_then_restore():
    """A nested scope is how a background task could act for one owner without
    disturbing the request it was spawned from."""
    with acting_as("request-owner"):
        with acting_as("other-owner"):
            assert current_owner() == "other-owner"
        assert current_owner() == "request-owner"
