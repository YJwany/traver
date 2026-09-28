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
from travel_api.providers.amap.place_resolver import (
    AMAP_PLACE_RESOLVER_NAME,
    AmapPlaceResolver,
    amap_confidence,
    amap_precision,
)
from travel_api.providers.amap.places import AmapPlaceProvider, amap_stable_id
from travel_api.providers.amap.routing import AmapRoutingProvider
from travel_api.providers.amap.weather import AmapWeatherProvider

__all__ = [
    "AMAP_KEY_CREDENTIAL",
    "AMAP_PLACE_PROVIDER_NAME",
    "AMAP_PLACE_RESOLVER_NAME",
    "AMAP_PROVIDER_ID",
    "AMAP_PROVIDER_NAMES",
    "AMAP_ROUTING_PROVIDER_NAME",
    "AMAP_WEATHER_PROVIDER_NAME",
    "DEFAULT_AMAP_BASE_URL",
    "AmapClient",
    "AmapError",
    "AmapGeocoder",
    "AmapKeyError",
    "AmapPlace",
    "AmapPlaceProvider",
    "AmapPlaceResolver",
    "AmapQuotaError",
    "AmapRoutingProvider",
    "AmapWeatherProvider",
    "amap_confidence",
    "amap_precision",
    "amap_stable_id",
    "build_amap_providers",
]

#: Registry names for the providers 高德 backs.
#:
#: Three distinct names rather than one shared ``"amap"``, because the registry keys
#: providers by name. Three objects sharing a name meant only the last one survived
#: registration — the routing and POI providers were silently discarded, so a keyed
#: deployment still planned keyless while the capability matrix, which is driven by
#: the manifest's *declaration*, went on advertising 公交/地铁 as covered. One name
#: per provider is what the rest of the registry assumes (openstreetmap, osrm,
#: open-meteo), so 高德 follows the same rule.
#:
#: ``AMAP_PROVIDER_ID`` remains the single *source* id: it is what the settings page
#: writes credentials against and what the manifest describes.
AMAP_ROUTING_PROVIDER_NAME = "amap-routing"
AMAP_PLACE_PROVIDER_NAME = "amap-places"
AMAP_WEATHER_PROVIDER_NAME = "amap-weather"
AMAP_PROVIDER_NAMES: tuple[str, ...] = (
    AMAP_ROUTING_PROVIDER_NAME,
    AMAP_PLACE_PROVIDER_NAME,
    AMAP_WEATHER_PROVIDER_NAME,
    # The place resolver. A fourth name because the registry keys providers by name, so
    # reusing ``amap-places`` would silently discard one of the two — the failure this tuple
    # exists to prevent.
    AMAP_PLACE_RESOLVER_NAME,
)


def build_amap_providers(
    *,
    key: str,
    http: Any,
    base_url: str = DEFAULT_AMAP_BASE_URL,
    timeout_seconds: float = 30.0,
    ttl_seconds: int = 300,
    max_route_points: int = 25,
) -> list[Any]:
    """Build the AMap-backed providers for one owner's key.

    Returns several objects sharing one client. The provider protocol is
    per-capability, so routing, POI search and weather are separate classes; they are
    grouped here because they share a key, a base URL and a rate limit, and splitting
    the client would multiply the requests each one independently throttles.

    Each gets its own registry name (see ``AMAP_PROVIDER_NAMES``). They must not
    share one: the registry is keyed by name, so a shared name would keep only the
    last provider and drop the rest without any error.

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
    geocoder = AmapGeocoder(client, name=AMAP_PLACE_PROVIDER_NAME)

    return [
        AmapRoutingProvider(
            client,
            name=AMAP_ROUTING_PROVIDER_NAME,
            ttl_seconds=ttl_seconds,
            max_points=max_route_points,
        ),
        AmapPlaceProvider(
            client,
            name=AMAP_PLACE_PROVIDER_NAME,
            ttl_seconds=ttl_seconds,
            geocoder=geocoder,
        ),
        AmapWeatherProvider(
            client,
            name=AMAP_WEATHER_PROVIDER_NAME,
            ttl_seconds=ttl_seconds,
            geocoder=geocoder,
        ),
        # Shares the geocoder with the weather provider: both need the trip's city, and one
        # instance means a trip resolves it once rather than once per capability.
        #
        # No cache TTL, for the same reason the geocoder has none: 高德's terms forbid storing
        # results, so every lookup is a live request against the owner's own quota.
        AmapPlaceResolver(client, geocoder=geocoder),
    ]
