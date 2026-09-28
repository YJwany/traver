"""Free, keyless data sources for the Travel Engine.

Everything in this package targets services that require **no API key, no
account and no paid tier**: OpenStreetMap (geocoding and POIs), Open-Meteo
(weather, air quality), OSRM (routing), Wikipedia/Wikidata (attraction text) and
the MIT-licensed ``holidays`` database (public holidays). Together they cover the
geo-fact and content-reference layers end to end.

What they deliberately do NOT provide is commercial fact: no room rates, no
availability, no negotiated prices. That gap is not worked around — the
``OverpassPoiProvider`` returns an empty offer set and the budget engine marks
the hotel component as an estimate. Commercial providers plug into the same
registry alongside these.

Note on source selection: an earlier candidate for holidays was the public
``date.nager.at`` API. It was rejected because its Terms of Service allow free
use only for "private or non-profit projects" and require sponsorship for
commercial use, which does not meet the "all free" requirement. The bundled
MIT-licensed ``holidays`` library is both unconditionally free and, on 2026
China data, strictly more complete (it includes 清明节 and the 调休 rest days
that the API omitted).
"""

from __future__ import annotations

from travel_api.providers.free.attribution import (
    HOLIDAYS_ATTRIBUTION,
    OPEN_DATA_ATTRIBUTIONS,
    WIKIPEDIA_ATTRIBUTION,
    Attribution,
    attribution_for,
    attributions_as_dicts,
)
from travel_api.providers.free.geo import (
    SOURCE_NOMINATIM,
    SOURCE_OPEN_METEO,
    GeoPlace,
    GeoResolutionError,
    GeoResolver,
)
from travel_api.providers.free.holidays_provider import (
    DEFAULT_HOLIDAY_LANGUAGE,
    HOLIDAYS_SOURCE,
    HolidaysLibraryProvider,
)
from travel_api.providers.free.http import DEFAULT_USER_AGENT, FreeHttpClient
from travel_api.providers.free.open_meteo import OpenMeteoWeatherProvider
from travel_api.providers.free.opening_hours import parse_opening_hours
from travel_api.providers.free.osrm import OsmRoutingProvider
from travel_api.providers.free.overpass import OverpassPoiProvider
from travel_api.providers.free.wikipedia import (
    WikipediaContentProvider,
    normalize_wikipedia_tag,
)

__all__ = [
    "DEFAULT_HOLIDAY_LANGUAGE",
    "DEFAULT_USER_AGENT",
    "HOLIDAYS_ATTRIBUTION",
    "HOLIDAYS_SOURCE",
    "OPEN_DATA_ATTRIBUTIONS",
    "SOURCE_NOMINATIM",
    "SOURCE_OPEN_METEO",
    "WIKIPEDIA_ATTRIBUTION",
    "Attribution",
    "FreeHttpClient",
    "GeoPlace",
    "GeoResolutionError",
    "GeoResolver",
    "HolidaysLibraryProvider",
    "OpenMeteoWeatherProvider",
    "OsmRoutingProvider",
    "OverpassPoiProvider",
    "WikipediaContentProvider",
    "attribution_for",
    "attributions_as_dicts",
    "build_free_providers",
    "normalize_wikipedia_tag",
    "parse_opening_hours",
]


def build_free_providers(
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    language: str = "zh",
    request_timeout_seconds: float = 25.0,
    timeout_seconds: float = 90.0,
    bbox_radius_degrees: float = 0.045,
    overpass_mirrors: list[str] | None = None,
    osrm_base_url: str | None = None,
    poi_ttl_seconds: int = 3600,
    weather_ttl_seconds: int = 900,
    retry_count: int = 3,
    retry_backoff_seconds: float = 0.5,
    nominatim_fallback: bool = True,
    air_quality_enabled: bool = True,
    holidays_enabled: bool = True,
    holidays_country_code: str | None = None,
    wikipedia_enabled: bool = True,
    wikipedia_ttl_seconds: int = 604800,
) -> tuple[list[object], FreeHttpClient]:
    """Construct the free provider stack.

    Two timeouts, deliberately:

    * ``request_timeout_seconds`` bounds a single HTTP request.
    * ``timeout_seconds`` bounds a whole provider call, which for Overpass may
      span several mirror attempts plus rate-limit waits and HTTP-level retries.
      Keeping the budget above attempts x request budget is what lets mirror
      fallback actually happen instead of being cut off after the first slow
      mirror.

    ``retry_count`` is retried inside the shared client with exponential backoff.
    These public endpoints intermittently refuse the first connection and succeed
    on a later attempt, so retrying here (rather than only in the provider
    runtime) is what makes real-data runs reliable.

    Returns the providers and the shared HTTP client so the caller can close it
    on shutdown. All providers share one client and one GeoResolver, so a
    destination is geocoded once per process.

    ``nominatim_fallback`` controls whether Nominatim is tried when Open-Meteo
    geocoding has no match (it adds a real administrative bounding box). Setting
    it to False restricts geocoding to the single Open-Meteo source.

    ``holidays_country_code`` overrides the country used for the holiday
    calendar; when omitted the destination is geocoded to learn it.
    """
    http = FreeHttpClient(
        user_agent=user_agent,
        timeout_seconds=request_timeout_seconds,
        retry_count=retry_count,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    geo_resolver = GeoResolver(
        http,
        language=language,
        bbox_radius_degrees=bbox_radius_degrees,
        nominatim_fallback_enabled=nominatim_fallback,
    )

    providers: list[object] = [
        OverpassPoiProvider(
            http=http,
            geo_resolver=geo_resolver,
            mirrors=overpass_mirrors,
            timeout_seconds=timeout_seconds,
            poi_ttl_seconds=poi_ttl_seconds,
        ),
        OsmRoutingProvider(
            http=http,
            **({"base_url": osrm_base_url} if osrm_base_url else {}),
            timeout_seconds=timeout_seconds,
        ),
        OpenMeteoWeatherProvider(
            http=http,
            geo_resolver=geo_resolver,
            timeout_seconds=timeout_seconds,
            ttl_seconds=weather_ttl_seconds,
            air_quality_enabled=air_quality_enabled,
        ),
    ]

    if holidays_enabled:
        providers.append(
            HolidaysLibraryProvider(
                geo_resolver=geo_resolver,
                language=DEFAULT_HOLIDAY_LANGUAGE,
                country_code=holidays_country_code,
            )
        )

    providers.append(
        WikipediaContentProvider(
            http=http,
            language=language,
            timeout_seconds=timeout_seconds,
            ttl_seconds=wikipedia_ttl_seconds,
            enabled=wikipedia_enabled,
        )
    )

    return providers, http
