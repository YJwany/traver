"""Tests for per-owner credential plumbing in ``TripService`` and the request path.

The registry is built once in the lifespan, but a key belongs to one owner. These
tests pin the rules that make that work, and each one corresponds to a bug shape that
is invisible without an explicit test:

* a plan made by an owner who has a key must use the credentialed provider, including
  when the plan is rebuilt by a revision such as changing the budget;
* an owner with nothing configured must not acquire another owner's provider;
* the ambient owner bound by the middleware must be reset, or the next request on the
  same worker inherits it.
"""

from __future__ import annotations

import pytest
from travel_api.providers.registry import ProviderRegistry
from travel_api.services.owner_context import acting_as, current_owner, resolve_owner


class _RecordingProvider:
    """A routing provider that records which key it was built with.

    Implements ``calculate_routes`` because capability routing is by ``isinstance``
    against a runtime-checkable protocol: a stub without the method is filtered out of
    ``providers_for``, so the test would pass vacuously against an empty list.
    """

    def __init__(self, name: str, key: str) -> None:
        self.name = name
        self.key = key

    async def calculate_routes(self, profile, points):  # pragma: no cover - not called
        raise NotImplementedError


class _NoopRuntime:
    async def call(self, provider, method, *args):  # pragma: no cover - unused
        raise NotImplementedError


def _registry(keys_by_owner: dict[str, str]) -> ProviderRegistry:
    def factory(owner_id):
        key = keys_by_owner.get(owner_id or "")
        return [_RecordingProvider("amap", key)] if key else []

    return ProviderRegistry([], runtime=_NoopRuntime(), credential_provider_factory=factory)


# -- resolve_owner is the decision point ----------------------------------


def test_an_explicit_owner_beats_the_ambient_one():
    """A background task acting for someone must not be overridden by whatever owner
    happened to be bound in the surrounding context."""
    with acting_as("ambient-owner"):
        assert resolve_owner("explicit-owner") == "explicit-owner"


def test_the_ambient_owner_is_used_when_the_caller_has_none():
    """The AI tool path cannot forward an owner: handler signatures are the published
    OpenAI tool contract, so the owner has to travel ambiently."""
    with acting_as("ambient-owner"):
        assert resolve_owner(None) == "ambient-owner"


def test_no_owner_anywhere_stays_none_rather_than_becoming_a_string():
    """``None`` must survive as ``None``. Coercing it to ``"anonymous"`` would make an
    unauthenticated request look like a real owner with its own credentials."""
    assert resolve_owner(None) is None


def test_an_explicit_empty_owner_is_preserved():
    """The anonymous owner is the empty string, not "absent". Promoting it to the
    ambient owner would hand an anonymous request someone else's key."""
    with acting_as("someone-with-a-key"):
        assert resolve_owner("") == ""


# -- the registry hands out the right providers ---------------------------


def test_each_owner_gets_the_provider_built_with_their_own_key():
    registry = _registry({"owner-a": "key-a", "owner-b": "key-b"})

    provider_a = registry.for_owner("owner-a").providers_for("calculate_routes")
    provider_b = registry.for_owner("owner-b").providers_for("calculate_routes")

    assert provider_a[0].key == "key-a"
    assert provider_b[0].key == "key-b"
    # And the instances are distinct: sharing one would mean one owner's key is used
    # for the other's requests.
    assert provider_a[0] is not provider_b[0]


def test_rebuilding_for_the_same_owner_reflects_a_changed_key():
    """The "I saved my key and it still uses the old one" bug.

    A cached provider would keep the first key forever. Building per call is what makes
    a rotation take effect on the next plan.
    """
    keys = {"owner-a": "first-key"}
    registry = _registry(keys)
    assert registry.for_owner("owner-a").providers_for("calculate_routes")[0].key == "first-key"

    keys["owner-a"] = "second-key"
    assert registry.for_owner("owner-a").providers_for("calculate_routes")[0].key == "second-key"


def test_an_owner_with_no_key_does_not_receive_another_owners_provider():
    """The isolation invariant at the registry level."""
    registry = _registry({"owner-a": "key-a"})

    assert registry.for_owner("owner-b").providers_for("calculate_routes") == []
    assert registry.for_owner(None).providers_for("calculate_routes") == []


def test_the_base_registry_is_never_mutated_by_scoping():
    """Scoping must not leak a credentialed provider into the shared registry, or the
    next owner would inherit it."""
    registry = _registry({"owner-a": "key-a"})
    registry.for_owner("owner-a")

    assert registry.all() == []
    assert registry.providers_for("calculate_routes") == []


# -- the ambient owner is scoped, not global ------------------------------


@pytest.mark.asyncio
async def test_concurrent_owners_do_not_observe_each_others_keys():
    """Two requests in flight at once, as on a real worker.

    This is the failure the ambient owner most invites: a module-level variable would
    be overwritten by whichever request started last, and one owner would plan with
    the other's key.
    """
    import asyncio

    registry = _registry({"owner-a": "key-a", "owner-b": "key-b"})
    seen: dict[str, str] = {}

    async def plan(owner: str, delay: float) -> None:
        with acting_as(owner):
            await asyncio.sleep(delay)
            # Read *after* yielding, so a shared global would already be wrong.
            provider = registry.for_owner(resolve_owner(None)).providers_for("calculate_routes")[0]
            seen[owner] = provider.key

    await asyncio.gather(plan("owner-a", 0.02), plan("owner-b", 0.0))

    assert seen == {"owner-a": "key-a", "owner-b": "key-b"}
    # Nothing is left bound afterwards.
    assert current_owner() is None


# -- the budget-revision path forwards the owner --------------------------


def test_changing_the_budget_must_forward_the_owner():
    """Guards the specific defect this test was written for.

    ``_change_budget`` rebuilds the entire preview. It originally called ``preview()``
    without an owner, relying on the ambient context — which works over HTTP but
    silently drops the credential anywhere else, and would quietly re-plan the trip
    with worse, keyless routes.
    """
    import inspect

    from travel_api.services.trip_service import TripService

    signature = inspect.signature(TripService._change_budget)
    assert "owner_id" in signature.parameters, (
        "_change_budget must accept owner_id so it can forward it to preview()"
    )

    source = inspect.getsource(TripService._change_budget)
    assert "owner_id=owner_id" in source, (
        "_change_budget must pass owner_id through to preview(), not rely on the ambient context"
    )
