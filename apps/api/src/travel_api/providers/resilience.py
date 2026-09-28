from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class ProviderCircuitOpenError(RuntimeError):
    """Raised when a provider is temporarily isolated after repeated failures."""


class ProviderTransientError(RuntimeError):
    """Retryable provider failure: network error, timeout, HTTP 429 or 5xx.

    Free public endpoints (Overpass, OSRM, Nominatim) fail this way routinely,
    so these failures must be retried rather than surfaced immediately.
    """


class ProviderPermanentError(RuntimeError):
    """Non-retryable provider failure: HTTP 4xx, malformed payload, bad request."""


#: Failures worth another attempt. Kept as a tuple so both this module and the
#: HTTP layer agree on what "transient" means.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
    ProviderTransientError,
)


@dataclass
class ProviderMetrics:
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    total_latency_ms: float = 0.0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = 0
    open_until: float | None = None

    @property
    def average_latency_ms(self) -> float | None:
        if self.success_count == 0:
            return None
        return self.total_latency_ms / self.success_count


@dataclass(frozen=True)
class ProviderCallPolicy:
    timeout_seconds: float = 5.0
    retry_count: int = 2
    backoff_seconds: float = 0.1
    circuit_breaker_threshold: int = 3
    circuit_breaker_reset_seconds: float = 30.0


class ProviderRuntime:
    """Shared timeout/retry/circuit-breaker runtime for all Provider calls."""

    def __init__(self, policy: ProviderCallPolicy | None = None) -> None:
        self.policy = policy or ProviderCallPolicy()
        self._metrics: dict[str, ProviderMetrics] = {}

    def metrics_for(self, provider: str) -> ProviderMetrics:
        return self._metrics.setdefault(provider, ProviderMetrics())

    def is_circuit_open(self, provider: str) -> bool:
        metrics = self.metrics_for(provider)
        if metrics.open_until is None:
            return False
        if time.monotonic() < metrics.open_until:
            return True
        # Half-open: allow the next call through to probe recovery.
        metrics.open_until = None
        return False

    async def call(self, provider: Any, method: str, *args: Any) -> Any:
        metrics = self.metrics_for(provider.name)
        if self.is_circuit_open(provider.name):
            raise ProviderCircuitOpenError(f"provider circuit open: {provider.name}")

        # A provider may declare its own budget. Public community endpoints
        # (Overpass especially) legitimately take many seconds, so a single
        # global timeout would either starve commercial providers or
        # systematically cut off the free ones.
        timeout = getattr(provider, "timeout_seconds", None) or self.policy.timeout_seconds

        last_error: BaseException | None = None
        for attempt in range(self.policy.retry_count + 1):
            metrics.request_count += 1
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(getattr(provider, method)(*args), timeout=timeout)
            except PermissionError:
                # Authorization failures are never retried and never open the
                # circuit: the request itself is not allowed.
                metrics.failure_count += 1
                metrics.last_failure_at = datetime.now(timezone.utc)
                raise
            except TRANSIENT_EXCEPTIONS as exc:
                metrics.failure_count += 1
                if isinstance(exc, asyncio.TimeoutError):
                    metrics.timeout_count += 1
                metrics.last_failure_at = datetime.now(timezone.utc)
                last_error = exc
            except Exception:
                # Permanent failures (HTTP 4xx, bad payload) and unexpected
                # provider bugs are recorded but never retried, and never trip
                # the circuit: the upstream service is not at fault.
                metrics.failure_count += 1
                metrics.last_failure_at = datetime.now(timezone.utc)
                raise
            else:
                metrics.success_count += 1
                metrics.consecutive_failures = 0
                metrics.last_success_at = datetime.now(timezone.utc)
                metrics.total_latency_ms += (time.perf_counter() - started) * 1000
                return result

            if attempt < self.policy.retry_count:
                await asyncio.sleep(self.policy.backoff_seconds * (2**attempt))

        # Retries are exhausted: this counts as ONE failed call. Counting each
        # attempt here would let a single flaky request trip the breaker.
        metrics.consecutive_failures += 1
        self._open_if_needed(metrics)

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"provider call failed without an error: {provider.name}.{method}")

    def _open_if_needed(self, metrics: ProviderMetrics) -> None:
        if metrics.consecutive_failures >= self.policy.circuit_breaker_threshold:
            metrics.open_until = time.monotonic() + self.policy.circuit_breaker_reset_seconds
