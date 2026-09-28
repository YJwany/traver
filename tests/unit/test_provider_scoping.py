"""Tests for per-owner provider scoping.

The registry is built once at startup, but a key belongs to one person. These tests
pin down the resulting rules, each of which was a real design decision:

* an owner with no credentials gets exactly the shared registry, so the
  zero-configuration path is untouched;
* credentialed providers are rebuilt per call, because a cached instance keeps the
  key it was built with — the "I rotated my key and it still uses the old one" bug;
* one owner's provider is invisible to another;
* a factory that raises degrades to the free stack rather than failing the plan,
  since a full itinerary is still producible without any credentials.
"""

from __future__ import annotations

import pytest
from travel_api.providers.registry import ProviderRegistry


class _Provider:
    """Minimal stand-in exposing the ``name`` the registry keys on."""

    def __init__(self, name: str, marker: str = "") -> None:
        self.name = name
        self.marker = marker


class _StubRuntime:
    """The runtime is irrelevant here; the registry only stores it."""

    async def call(self, provider, method, *args):  # pragma: no cover - unused
        raise NotImplementedError


def _registry(factory=None, providers=()):
    return ProviderRegistry(
        list(providers), runtime=_StubRuntime(), credential_provider_factory=factory
    )


# -- the zero-credential path is untouched ---------------------------------


def test_no_factory_returns_the_same_registry():
    """Without a factory there is nothing to add, so no copy is made.

    Asserted by identity rather than equality: the point is that the common path
    allocates nothing and cannot diverge from the base registry.
    """
    base = _registry()
    assert base.for_owner("owner-a") is base
    assert base.for_owner(None) is base


def test_an_owner_with_no_credentials_gets_the_shared_registry():
    """Zero configuration must keep working; this is the 渐进增强 guarantee.

    Returning ``self`` matters beyond allocation: the caller may compare identity to
    detect that nothing was unlocked.
    """
    base = _registry(factory=lambda owner: [])
    assert base.for_owner("owner-a") is base


# -- credentialed providers -------------------------------------------------


def test_a_factory_result_is_added_for_that_owner():
    base = _registry(factory=lambda owner: [_Provider("amap")] if owner == "owner-a" else [])
    scoped = base.for_owner("owner-a")

    assert {provider.name for provider in scoped.all()} == {"amap"}
    # And the base registry is unaffected — scoping must not mutate shared state.
    assert base.all() == []


def test_the_shared_providers_are_still_present():
    base = _registry(
        factory=lambda owner: [_Provider("amap")],
        providers=[_Provider("openstreetmap"), _Provider("open-meteo")],
    )
    names = {provider.name for provider in base.for_owner("owner-a").all()}
    assert names == {"openstreetmap", "open-meteo", "amap"}


def test_the_runtime_is_shared_so_metrics_and_circuit_breakers_are_not_split():
    """A fresh runtime per owner would reset the circuit breaker and the request
    counters on every request, hiding a failing provider."""
    base = _registry(factory=lambda owner: [_Provider("amap")])
    assert base.for_owner("owner-a").runtime is base.runtime


def test_one_owners_provider_is_absent_for_another():
    base = _registry(factory=lambda owner: [_Provider("amap", owner)] if owner == "owner-a" else [])
    assert "amap" not in {provider.name for provider in base.for_owner("owner-b").all()}
    assert "amap" not in {provider.name for provider in base.for_owner(None).all()}


def test_the_factory_is_called_once_per_scoping():
    """It reads the vault, so it must not be called more than needed — but it must
    be called again on the next request, which is what makes a new key take effect.
    """
    calls: list[str | None] = []

    def factory(owner):
        calls.append(owner)
        return [_Provider("amap")]

    base = _registry(factory=factory)
    base.for_owner("owner-a")
    base.for_owner("owner-a")
    assert calls == ["owner-a", "owner-a"]


def test_a_credentialed_provider_replaces_one_of_the_same_name():
    """A keyed 高德 is a strictly better implementation of the same source.

    Raising on the clash would be worse than replacing: the scoped registry is
    built with the shared providers, so a duplicate name would abort planning
    instead of simply using the keyed provider.
    """
    base = _registry(
        factory=lambda owner: [_Provider("amap", "keyed")],
        providers=[_Provider("amap", "unkeyed")],
    )
    scoped = base.for_owner("owner-a")
    assert [provider for provider in scoped.all() if provider.name == "amap"][0].marker == "keyed"
    assert len(scoped.all()) == 1


def test_a_nameless_provider_is_skipped_rather_than_breaking_the_registry():
    """Registration requires a name; a malformed factory result must not take the
    whole plan down when the free sources could still answer."""
    base = _registry(factory=lambda owner: [object(), _Provider("amap")])
    names = {provider.name for provider in base.for_owner("owner-a").all()}
    assert names == {"amap"}


# -- failure handling ------------------------------------------------------


def test_a_raising_factory_degrades_to_the_free_stack():
    """A locked vault or a bad stored value must not make planning impossible.

    Without a key the free sources still produce a complete itinerary, so the
    correct response is to carry on with them.
    """

    def factory(owner):
        raise RuntimeError("vault is locked")

    base = _registry(factory=factory, providers=[_Provider("openstreetmap")])
    scoped = base.for_owner("owner-a")
    assert scoped is base
    assert {provider.name for provider in scoped.all()} == {"openstreetmap"}


@pytest.mark.parametrize("owner", ["owner-a", "", None])
def test_scoping_accepts_every_owner_shape(owner):
    """``None`` is "auth off", ``""`` is the anonymous owner. Both must work and
    must not be conflated."""
    base = _registry(factory=lambda value: [_Provider("amap", value or "anon")])
    assert "amap" in {provider.name for provider in base.for_owner(owner).all()}


def test_the_factory_can_be_attached_after_construction():
    """Startup order: the registry is built with the free stack, and the credential
    resolver it needs does not exist until later in the lifespan."""
    base = _registry()
    assert base.for_owner("owner-a") is base

    base.set_credential_provider_factory(lambda owner: [_Provider("amap")])
    assert "amap" in {provider.name for provider in base.for_owner("owner-a").all()}

    base.set_credential_provider_factory(None)
    assert base.for_owner("owner-a") is base
