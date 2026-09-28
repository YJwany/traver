"""Provider runtime retry / circuit-breaker / compliance behaviour."""

from __future__ import annotations

import asyncio

import pytest
from travel_api.domain.models import ProviderHealth, ProviderResult
from travel_api.providers.registry import ProviderRegistry
from travel_api.providers.resilience import (
    ProviderCallPolicy,
    ProviderCircuitOpenError,
    ProviderPermanentError,
    ProviderRuntime,
    ProviderTransientError,
)


class FlakyProvider:
    name = "flaky"

    def __init__(self, failures: int = 1) -> None:
        self.calls = 0
        self.failures = failures

    async def health(self):
        return ProviderHealth(provider=self.name, status="healthy")

    async def search_hotels(self, profile):
        self.calls += 1
        if self.calls <= self.failures:
            raise asyncio.TimeoutError
        return ProviderResult(provider=self.name)


class DeadProvider:
    name = "dead"

    def __init__(self) -> None:
        self.calls = 0

    async def health(self):
        return ProviderHealth(provider=self.name, status="healthy")

    async def search_hotels(self, profile):
        self.calls += 1
        raise ProviderTransientError("upstream busy")


class MisconfiguredProvider:
    name = "misconfigured"

    async def health(self):
        return ProviderHealth(provider=self.name, status="healthy")

    async def search_hotels(self, profile):
        raise ProviderPermanentError("HTTP 403 from upstream")


@pytest.mark.asyncio
async def test_provider_runtime_retries_timeout_and_records_health():
    provider = FlakyProvider(failures=1)
    registry = ProviderRegistry(
        [provider],
        runtime=ProviderRuntime(
            ProviderCallPolicy(
                timeout_seconds=0.5,
                retry_count=1,
                backoff_seconds=0,
                circuit_breaker_threshold=3,
            )
        ),
    )

    result = await registry.call(provider, "search_hotels", None)
    health = (await registry.health())[0]

    assert result.status == "success"
    assert provider.calls == 2
    # The call eventually succeeded, so it must not look degraded.
    assert health.status == "healthy"
    assert health.failure_count == 1  # attempts are still counted
    assert health.timeout_count == 1


@pytest.mark.asyncio
async def test_retries_within_one_call_do_not_trip_the_circuit():
    """A single failing call must count as ONE failure, not one per attempt."""
    provider = DeadProvider()
    runtime = ProviderRuntime(
        ProviderCallPolicy(
            timeout_seconds=0.5,
            retry_count=2,  # three attempts per call
            backoff_seconds=0,
            circuit_breaker_threshold=2,
        )
    )

    with pytest.raises(ProviderTransientError):
        await runtime.call(provider, "search_hotels", None)

    metrics = runtime.metrics_for(provider.name)
    assert metrics.request_count == 3
    assert metrics.consecutive_failures == 1
    assert not runtime.is_circuit_open(provider.name)

    # A second failing call now reaches the threshold.
    with pytest.raises(ProviderTransientError):
        await runtime.call(provider, "search_hotels", None)
    assert runtime.is_circuit_open(provider.name)


@pytest.mark.asyncio
async def test_success_resets_the_consecutive_failure_count():
    provider = FlakyProvider(failures=1)
    runtime = ProviderRuntime(
        ProviderCallPolicy(retry_count=1, backoff_seconds=0, circuit_breaker_threshold=2)
    )
    await runtime.call(provider, "search_hotels", None)
    assert runtime.metrics_for(provider.name).consecutive_failures == 0


@pytest.mark.asyncio
async def test_permanent_failures_are_not_retried_and_do_not_open_the_circuit():
    provider = MisconfiguredProvider()
    runtime = ProviderRuntime(
        ProviderCallPolicy(retry_count=3, backoff_seconds=0, circuit_breaker_threshold=1)
    )
    with pytest.raises(ProviderPermanentError):
        await runtime.call(provider, "search_hotels", None)
    metrics = runtime.metrics_for(provider.name)
    assert metrics.request_count == 1, "a 4xx must not be retried"
    assert not runtime.is_circuit_open(provider.name)


@pytest.mark.asyncio
async def test_open_circuit_short_circuits_further_calls():
    provider = DeadProvider()
    runtime = ProviderRuntime(
        ProviderCallPolicy(
            retry_count=0,
            backoff_seconds=0,
            circuit_breaker_threshold=1,
            circuit_breaker_reset_seconds=60,
        )
    )
    with pytest.raises(ProviderTransientError):
        await runtime.call(provider, "search_hotels", None)
    calls_before = provider.calls
    with pytest.raises(ProviderCircuitOpenError):
        await runtime.call(provider, "search_hotels", None)
    assert provider.calls == calls_before, "the provider must not be called while open"


def test_compliance_rate_limits_match_published_policies():
    """Nominatim and the OSRM demo server both cap at 1 request/second."""
    from travel_api.providers.free.http import DEFAULT_HOST_INTERVALS

    assert DEFAULT_HOST_INTERVALS["nominatim.openstreetmap.org"] >= 1.0
    assert DEFAULT_HOST_INTERVALS["router.project-osrm.org"] >= 1.0
    assert DEFAULT_HOST_INTERVALS["overpass-api.de"] >= 1.0


def test_overpass_mirrors_exclude_the_retired_kumi_instance():
    from travel_api.providers.free.overpass import OVERPASS_MIRRORS

    assert not any("kumi.systems" in mirror for mirror in OVERPASS_MIRRORS)
    assert len(OVERPASS_MIRRORS) >= 2, "mirror fallback needs alternatives"


def test_every_open_data_source_carries_a_licence_credit():
    from travel_api.providers.free.attribution import attributions_as_dicts

    credits = attributions_as_dicts()
    providers = {item["provider"] for item in credits}
    assert {"openstreetmap", "open-meteo", "osrm"} <= providers
    for credit in credits:
        assert credit["text"]
        assert credit["url"].startswith("https://")
        assert credit["licence"]


def test_user_agent_identifies_the_application():
    from travel_api.providers.free.http import DEFAULT_USER_AGENT

    # Stock library user agents are explicitly rejected by these services.
    assert "Traver" in DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.lower() not in {"python-httpx", "python-requests"}
