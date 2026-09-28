from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from travel_api.domain.models import ProviderHealth
from travel_api.providers.base import (
    CAPABILITY_BY_METHOD,
    HealthProvider,
    unserved_capabilities_of,
)
from travel_api.providers.resilience import ProviderCallPolicy, ProviderRuntime

logger = logging.getLogger(__name__)

#: Given an owner, return the providers that owner's credentials unlock. Called on
#: every request rather than once, so a key saved in the settings page takes effect
#: immediately.
CredentialProviderFactory = Callable[[str | None], list[Any]]


class ProviderRegistry:
    """Registers providers and routes each capability call to the providers that
    actually declare it.

    Free sources are deliberately partial: OpenStreetMap supplies geo facts but
    no prices, Open-Meteo supplies weather only. Capability routing lets those
    coexist with commercial providers without any provider faking a category it
    cannot serve.

    ``credential_provider_factory`` handles the awkward case this design creates:
    the registry is built once at startup, but a key belongs to one owner. See
    ``for_owner``.
    """

    def __init__(
        self,
        providers: Iterable[Any] = (),
        runtime: ProviderRuntime | None = None,
        credential_provider_factory: CredentialProviderFactory | None = None,
    ) -> None:
        self._providers: dict[str, Any] = {}
        self.runtime = runtime or ProviderRuntime(ProviderCallPolicy())
        self._credential_provider_factory = credential_provider_factory
        for provider in providers:
            self.register(provider)

    # -- per-owner scoping -------------------------------------------------
    def set_credential_provider_factory(self, factory: CredentialProviderFactory | None) -> None:
        """Attach the per-owner provider factory after construction.

        The registry is built with the free stack, but the factory needs the
        credential resolver, which is built later in the lifespan. A setter rather
        than constructor injection avoids reordering unrelated startup steps and
        keeps the failure mode obvious: with no factory set, ``for_owner`` returns
        the shared registry, so the app runs exactly as it did before credentials
        existed.
        """
        self._credential_provider_factory = factory

    def for_owner(self, owner_id: str | None) -> ProviderRegistry:
        """A view of this registry plus whatever ``owner_id`` has unlocked.

        Needed because data sources are now per-owner: 高德's key belongs to one
        person, while the free sources are shared. The registry itself is built
        once in the lifespan, so the credentialed providers are *constructed here,
        per call*, and deliberately never cached.

        Caching them would be the "I saved my key but it still uses the old one"
        bug: a provider holds the key it was built with, so a cached instance
        survives a rotation or a removal. Rebuilding costs one small object per
        request — the HTTP client, rate limiter and connection pool are all shared
        — which is cheap next to the network calls it makes.

        Returns ``self`` when there is nothing to add, so the common case (no
        credentials configured at all, which must keep working) allocates nothing.
        """
        if self._credential_provider_factory is None:
            return self
        try:
            extra = self._credential_provider_factory(owner_id)
        except Exception as exc:
            # A broken credential must not take planning down: with no 高德 key the
            # free sources still produce a complete itinerary. Reported, not raised.
            logger.warning(
                "credential-backed providers unavailable for owner=%r: %s", owner_id, exc
            )
            return self
        if not extra:
            return self

        scoped = ProviderRegistry(self._providers.values(), runtime=self.runtime)
        for provider in extra:
            name = getattr(provider, "name", None)
            if not name:
                logger.warning("skipping credential-backed provider without a name")
                continue
            # Overrides rather than raising on a name clash: the credentialed
            # provider is the more specific implementation of the same source (a
            # keyed 高德 replaces an unkeyed one), and refusing to register it would
            # silently fall back to the weaker source.
            scoped._providers[name] = provider
        return scoped

    def register(self, provider: Any) -> None:
        name = getattr(provider, "name", None)
        if not name:
            raise ValueError("provider must expose a non-empty 'name'")
        if name in self._providers:
            raise ValueError(f"provider already registered: {name}")
        self._providers[name] = provider

    def get(self, name: str) -> Any:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown provider: {name}") from exc

    def all(self) -> list[Any]:
        return list(self._providers.values())

    def providers_for(self, method: str) -> list[Any]:
        """Providers able to serve `method`, in registration order.

        Structural: a provider qualifies by implementing the method's protocol. This is what
        dispatch uses, and it must stay permissive — a source with no room rates still has to
        be asked for room rates, because returning an empty success is how it declines
        honestly and how the request shows up in the provider audit.

        Unknown methods fall back to every provider so custom capabilities keep
        working without a registry change.
        """
        capability = CAPABILITY_BY_METHOD.get(method)
        providers = list(self._providers.values())
        if capability is None:
            return providers
        return [provider for provider in providers if isinstance(provider, capability)]

    def serving_providers_for(self, method: str) -> list[Any]:
        """Providers that declare they actually provide data for `method`.

        The distinction from :meth:`providers_for` is the point. Dispatch asks "who can be
        called", and a stub that always returns an empty list qualifies. *Reporting* asks
        "who will answer", and that stub must not.

        Conflating the two is not hypothetical: the capability matrix credited OpenStreetMap
        with fully covering room offers, because it implements ``search_room_offers``, while
        its own caveat said OSM has no rates. A deployment with no source that can produce a
        room rate advertised that room rates were available. Use this for the catalogue and
        anything user-facing; use :meth:`providers_for` for calling.
        """
        return [
            provider
            for provider in self.providers_for(method)
            if method not in unserved_capabilities_of(provider)
        ]

    async def call(self, provider: Any, method: str, *args):
        return await self.runtime.call(provider, method, *args)

    async def health(self) -> list[ProviderHealth]:
        results = []
        for provider in self._providers.values():
            if isinstance(provider, HealthProvider):
                base = await provider.health()
            else:
                base = ProviderHealth(provider=provider.name, status="unknown")
            metrics = self.runtime.metrics_for(provider.name)
            if metrics.open_until is not None:
                status = "unavailable"
                detail = "circuit breaker is open"
            elif metrics.consecutive_failures:
                # Degraded reflects the *current* state. Using the cumulative
                # failure_count here would mark a fully recovered provider as
                # degraded for the rest of the process lifetime; the running
                # totals stay visible in the metric fields below.
                status = "degraded"
                detail = (
                    f"consecutive_failures={metrics.consecutive_failures}, "
                    f"timeouts={metrics.timeout_count}"
                )
            else:
                status = base.status
                detail = base.detail
            results.append(
                base.model_copy(
                    update={
                        "status": status,
                        "latency_ms": metrics.average_latency_ms or base.latency_ms,
                        "detail": detail,
                        "request_count": metrics.request_count,
                        "success_count": metrics.success_count,
                        "failure_count": metrics.failure_count,
                        "timeout_count": metrics.timeout_count,
                        "last_success_at": metrics.last_success_at,
                        "last_failure_at": metrics.last_failure_at,
                    }
                )
            )
        return results
