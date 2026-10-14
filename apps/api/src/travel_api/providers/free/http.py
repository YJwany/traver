"""Shared async HTTP layer for the free, public data sources.

The public OpenStreetMap / Open-Meteo / OSRM endpoints are shared community
infrastructure with published etiquette. This module centralises the three
things that keep us compliant and reliable:

1. **Per-host rate limiting** so we never exceed a service's request budget.
2. **Mirror fallback** because the Overpass main instance returns HTTP 504
   ("server is probably too busy") under load and must be retried elsewhere.
3. **Error classification** into transient vs permanent so the provider runtime
   retries only what is worth retrying.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from travel_api.providers.resilience import ProviderPermanentError, ProviderTransientError

#: Statuses that mean "try again": rate limiting and upstream failure.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_USER_AGENT = "TraverTravelPlanner/0.1 (+https://example.invalid/traver)"

#: Minimum seconds between two requests to the same host. These are compliance
#: values, not performance tuning:
#:   * Nominatim's usage policy allows an absolute maximum of 1 request/second.
#:   * FOSSGIS, which runs the routing server used by default, says "do not exceed 1
#:     request per second". Listed explicitly rather than left to ``FALLBACK_HOST_INTERVAL``:
#:     a licence constraint should not depend on a default that someone may retune.
#:   * The OSRM demo server publishes the same 1 request/second limit.
#:   * Open-Meteo's free tier allows 600/minute, so a small gap is ample.
#:   * Wikimedia asks bots to make requests serially, never in parallel.
DEFAULT_HOST_INTERVALS: dict[str, float] = {
    "nominatim.openstreetmap.org": 1.1,
    "overpass-api.de": 1.0,
    "overpass.private.coffee": 1.0,
    "maps.mail.ru": 1.0,
    "routing.openstreetmap.de": 1.0,
    "router.project-osrm.org": 1.0,
    "api.open-meteo.com": 0.2,
    "air-quality-api.open-meteo.com": 0.2,
    "geocoding-api.open-meteo.com": 0.2,
    "zh.wikipedia.org": 1.0,
    "www.wikidata.org": 1.0,
}
FALLBACK_HOST_INTERVAL = 1.0


class HostRateLimiter:
    """Serialises calls to one host so consecutive requests keep a minimum gap."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._lock = asyncio.Lock()
        self._last_call_at: float = 0.0

    async def acquire(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        async with self._lock:
            elapsed = time.monotonic() - self._last_call_at
            wait_for = self.min_interval_seconds - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_call_at = time.monotonic()


class FreeHttpClient:
    """Rate-limited JSON client for free public APIs."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 30.0,
        host_intervals: dict[str, float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_count: int = 3,
        retry_backoff_seconds: float = 0.5,
        retry_max_backoff_seconds: float = 4.0,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self._host_intervals = {**DEFAULT_HOST_INTERVALS, **(host_intervals or {})}
        self._limiters: dict[str, HostRateLimiter] = {}
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # Retries live here rather than only in the provider runtime because this
        # layer already knows the per-host rate limit, so backoff stays compliant
        # (every attempt is serialised through the limiter) and transient
        # first-connection failures are absorbed before they reach a provider.
        self.retry_count = max(0, retry_count)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.retry_max_backoff_seconds = max(0.0, retry_max_backoff_seconds)

    # -- lifecycle ---------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                transport=self._transport,
                headers={
                    # Verified against the live service: Nominatim answers HTTP 403
                    # "Access denied" when the User-Agent contains an e-mail
                    # address, and also rejects httpx's default UA. A plain token
                    # or a "+https://..." contact URL is accepted.
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def limiter_for(self, url: str) -> HostRateLimiter:
        host = httpx.URL(url).host or ""
        if host not in self._limiters:
            interval = self._host_intervals.get(host, FALLBACK_HOST_INTERVAL)
            self._limiters[host] = HostRateLimiter(interval)
        return self._limiters[host]

    # -- requests ----------------------------------------------------------
    async def _retrying(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """Run one logical request, retrying transient failures.

        Raises ProviderTransientError for retryable conditions and
        ProviderPermanentError otherwise. Permanent errors are never retried:
        retrying a 404 or a malformed query only wastes a shared service's budget.
        """
        last_error: ProviderTransientError | None = None
        for attempt in range(self.retry_count + 1):
            try:
                return await operation()
            except ProviderTransientError as exc:
                last_error = exc
                if attempt >= self.retry_count:
                    break
                delay = min(
                    self.retry_backoff_seconds * (2**attempt), self.retry_max_backoff_seconds
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Perform one logical request and parse the body as JSON."""

        async def _call() -> Any:
            response = await self._attempt_response(method, url, params=params, data=data)
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderPermanentError(f"{url} returned a non-JSON body") from exc

        return await self._retrying(_call)

    async def request_raw(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One logical request, retried on transient failure, body left unparsed.

        Needed for a service that does not answer ``application/json``. An MCP server
        replies with ``text/event-stream``, and ``response.json()`` on that body raises
        even though the request succeeded — so a caller that must read the raw text, or
        set a header the client does not default to, has no way through
        :meth:`request_json`.
        """

        async def _call() -> httpx.Response:
            return await self._attempt_response(
                method, url, params=params, data=data, json_body=json_body, headers=headers
            )

        return await self._retrying(_call)

    async def _attempt_response(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        await self.limiter_for(url).acquire()
        client = self._get_client()
        try:
            response = await client.request(
                method, url, params=params, data=data, json=json_body, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(f"{url} timed out: {type(exc).__name__}: {exc!r}") from exc
        except httpx.TransportError as exc:
            # Connection reset, DNS failure, TLS EOF: all retryable. httpx often
            # leaves str(exc) empty, so the class name and repr are included.
            raise ProviderTransientError(
                f"{url} transport error: {type(exc).__name__}: {exc!r}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderPermanentError(
                f"{url} request failed: {type(exc).__name__}: {exc!r}"
            ) from exc

        if response.status_code in RETRYABLE_STATUS:
            raise ProviderTransientError(f"{url} returned retryable status {response.status_code}")
        if response.status_code >= 400:
            raise ProviderPermanentError(
                f"{url} returned status {response.status_code}: {response.text[:200]}"
            )
        return response

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self.request_json("GET", url, params=params)

    async def post_json(self, url: str, *, data: dict[str, Any] | None = None) -> Any:
        return await self.request_json("POST", url, data=data)

    async def request_json_with_fallback(
        self,
        method: str,
        urls: list[str],
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> tuple[Any, str]:
        """Try each URL in order; return the first success and the URL used.

        Used for services with interchangeable public mirrors. Only the *last*
        failure is raised, so a single busy mirror does not fail the call.
        """
        if not urls:
            raise ProviderPermanentError("no endpoint configured")
        last_error: BaseException | None = None
        for url in urls:
            try:
                payload = await self.request_json(method, url, params=params, data=data)
            except (ProviderTransientError, ProviderPermanentError) as exc:
                last_error = exc
                continue
            return payload, url
        assert last_error is not None
        raise last_error
