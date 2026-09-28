from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from travel_api.domain.models import ProviderHealth
from travel_api.providers.base import CAPABILITY_BY_METHOD, HealthProvider
from travel_api.providers.resilience import ProviderCallPolicy, ProviderRuntime


class ProviderRegistry:
    """Registers providers and routes each capability call to the providers that
    actually declare it.

    Free sources are deliberately partial: OpenStreetMap supplies geo facts but
    no prices, Open-Meteo supplies weather only. Capability routing lets those
    coexist with commercial providers without any provider faking a category it
    cannot serve.
    """

    def __init__(
        self,
        providers: Iterable[Any] = (),
        runtime: ProviderRuntime | None = None,
    ) -> None:
        self._providers: dict[str, Any] = {}
        self.runtime = runtime or ProviderRuntime(ProviderCallPolicy())
        for provider in providers:
            self.register(provider)

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

        Unknown methods fall back to every provider so custom capabilities keep
        working without a registry change.
        """
        capability = CAPABILITY_BY_METHOD.get(method)
        providers = list(self._providers.values())
        if capability is None:
            return providers
        return [provider for provider in providers if isinstance(provider, capability)]

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
