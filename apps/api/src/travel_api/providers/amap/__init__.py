"""AMap provider package.

``build_amap_providers`` is the one entry point the application uses. It is called
**per request** with the owner's own key, never once at startup, because the key
belongs to an owner: a provider built once would keep whichever key it was first
constructed with, so a key saved in the settings page would appear not to work and a
removed key would keep being used.

Everything here requires a key. With none configured the package is never built and
the application runs on the free sources alone, which is the intended default — AMap
is an upgrade, not a dependency.
"""

from __future__ import annotations

from typing import Any

from travel_api.providers.amap.client import (
    AMAP_KEY_CREDENTIAL,
    AMAP_PROVIDER_ID,
    DEFAULT_AMAP_BASE_URL,
    AmapClient,
    AmapError,
    AmapKeyError,
    AmapQuotaError,
)
from travel_api.providers.amap.geocode import AmapGeocoder, AmapPlace
from travel_api.providers.amap.places import AmapPlaceProvider, amap_stable_id
from travel_api.providers.amap.routing import AmapRoutingProvider
from travel_api.providers.amap.weather import AmapWeatherProvider

__all__ = [
    "AMAP_KEY_CREDENTIAL",
    "AMAP_PROVIDER_ID",
    "DEFAULT_AMAP_BASE_URL",
    "AmapClient",
    "AmapError",
    "AmapGeocoder",
    "AmapKeyError",
    "AmapPlace",
    "AmapPlaceProvider",
    "AmapQuotaError",
    "AmapRoutingProvider",
    "AmapWeatherProvider",
    "amap_stable_id",
    "build_amap_providers",
]


def build_amap_providers(
    *,
    key: str,
    http: Any,
    base_url: str = DEFAULT_AMAP_BASE_URL,
    timeout_seconds: float = 30.0,
    ttl_seconds: int = 300,
    name: str = AMAP_PROVIDER_ID,
) -> list[Any]:
    """Build the AMap-backed providers for one owner's key.

    Returns several objects sharing one client. The provider protocol is
    per-capability, so routing, POI search and weather are separate classes; they are
    grouped here because they share a key, a base URL and a rate limit, and splitting
    the client would multiply the requests each one independently throttles.

    The geocoder is shared with the weather provider because AMap's weather endpoint
    is keyed by ``adcode``, which only the geocoder can produce. Passing the same
    instance means a trip resolves its city once rather than once per capability.

    Raises :class:`AmapKeyError` for a blank key. Callers should treat that as "not
    configured" rather than a failure: it means the owner has not supplied one, and
    the free sources already cover their trip.
    """
    client = AmapClient(
        http=http,
        key=key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    # No cache TTL: AMap's terms forbid storing results, so geocoding is live too.
    geocoder = AmapGeocoder(client, name=name)

    return [
        AmapRoutingProvider(client, name=name, ttl_seconds=ttl_seconds),
        AmapPlaceProvider(client, name=name, ttl_seconds=ttl_seconds, geocoder=geocoder),
        AmapWeatherProvider(client, name=name, ttl_seconds=ttl_seconds, geocoder=geocoder),
    ]
