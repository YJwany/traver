"""Destination geocoding for the free providers.

Every free source here is coordinate-based (Open-Meteo forecast, Overpass POI
search, OSRM routing), so a destination name such as "成都" must be resolved to
a point and a bounded search area first.

Open-Meteo's geocoding endpoint is used first because it is keyless, fast, and
returns the IANA timezone that the forecast endpoint needs. Nominatim is an
optional fallback that additionally supplies a real administrative bounding box.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from travel_api.domain.models import Coordinates, DataDomain, SourceReference, SourceType
from travel_api.providers.free.attribution import (
    OPEN_METEO_ATTRIBUTION as _OPEN_METEO,
)
from travel_api.providers.free.attribution import (
    OSM_ATTRIBUTION as _OSM,
)
from travel_api.providers.free.http import FreeHttpClient

logger = logging.getLogger(__name__)

OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

#: Human-readable credit strings carried in SourceReference.request_context so a
#: reference is self-describing even outside the API layer.
OSM_ATTRIBUTION = f"{_OSM.text} ({_OSM.licence})"
OPEN_METEO_ATTRIBUTION = f"{_OPEN_METEO.text} ({_OPEN_METEO.licence})"


@dataclass(frozen=True)
class GeoPlace:
    """A resolved destination plus the search area the POI queries will use."""

    name: str
    city: str
    country: str | None
    #: ISO 3166-1 alpha-2, upper-cased. Carried because services keyed by
    #: country (public holidays) need the code, and re-deriving it from the
    #: localized country name would be guesswork.
    country_code: str | None
    latitude: float
    longitude: float
    timezone: str | None
    #: (min_lat, min_lon, max_lat, max_lon) — always clamped to a small area so
    #: Overpass queries stay cheap.
    bbox: tuple[float, float, float, float]
    source_reference: SourceReference

    @property
    def coordinates(self) -> Coordinates:
        return Coordinates(latitude=self.latitude, longitude=self.longitude)

    @property
    def bbox_string(self) -> str:
        """Overpass bbox order: south,west,north,east."""
        min_lat, min_lon, max_lat, max_lon = self.bbox
        return f"{min_lat:.5f},{min_lon:.5f},{max_lat:.5f},{max_lon:.5f}"


class GeoResolutionError(LookupError):
    """Raised when a destination cannot be geocoded by any configured source."""


def _clamp_bbox(
    latitude: float, longitude: float, radius_degrees: float
) -> tuple[float, float, float, float]:
    return (
        latitude - radius_degrees,
        longitude - radius_degrees,
        latitude + radius_degrees,
        longitude + radius_degrees,
    )


#: GeoNames feature codes, most significant first. These are the levels that
#: actually occur as geocoding candidates.
_PPLC = "PPLC"  # capital of a political entity
_PPLA = "PPLA"  # seat of a first-order administrative division
_PPLA_LOWER = "PPLA2"  # seat of a second-order (and lower) division
_PPL = "PPL"  # populated place


def _feature_rank(feature_code: str) -> int:
    """Rank a GeoNames feature code by how likely it is the intended city.

    ``PPLC`` (a national capital) must outrank ``PPLA2`` (a county seat). The
    previous implementation tested ``startswith("PPLA")`` as the top rank, which
    silently demoted *every capital*: ``"PPLC"`` does not start with ``"PPLA"``,
    so Paris, France (PPLC, population 2.1M) ranked 1 while Paris, Texas
    (PPLA2, population 24k) ranked 2. Because the score tuple compares rank
    before population, the small town won and the resolver returned a point
    ~8,300 km away — with a plausible-looking place record and no error.
    """
    if feature_code == _PPLC:
        return 4
    if feature_code == _PPLA:
        return 3
    if feature_code.startswith(_PPLA_LOWER):
        return 2
    if feature_code.startswith(_PPL):
        return 1
    return 0


def _score_open_meteo_result(result: dict) -> tuple[int, float]:
    """Prefer administrative seats, then population.

    Open-Meteo happily returns a village named like a major city; ranking by
    feature code and population keeps the well-known place first.
    """
    feature_code = str(result.get("feature_code") or "")
    return _feature_rank(feature_code), float(result.get("population") or 0)


#: Confidence attached to a geocode, and the level below which another source is
#: consulted. A rank-1 ``PPL`` with no population is the case that matters: it
#: is how Open-Meteo reports a small town, and it is also how it reports a
#: *wrong* small town. Treating that as provisional is what lets the more precise
#: source override it.
_CONFIDENT_GEOCODE = 0.9
_PROVISIONAL_GEOCODE = 0.5
_MINIMUM_TRUSTED_CONFIDENCE = 0.8


SOURCE_OPEN_METEO = "open-meteo"
SOURCE_NOMINATIM = "nominatim"

#: Ask for several candidates rather than one, so a settlement point can be
#: preferred over an administrative boundary (see `_pick_nominatim_result`).
#: This costs nothing extra: it is the same single request.
_NOMINATIM_LIMIT = 5

#: Nominatim result types that denote an actual settlement *point*.
#:
#: The discriminator is ``addresstype`` (falling back to ``type``), not
#: ``class``: the live jsonv2 responses for these queries carry no ``class``
#: field at all, so keying off it would never match. An area type such as
#: ``county`` reports the centroid of the area, which can sit well away from the
#: town it is named after.
_NOMINATIM_SETTLEMENT_TYPES = frozenset(
    {"city", "town", "village", "hamlet", "municipality", "borough"}
)

#: How far a settlement point may sit from the top-ranked result and still count
#: as a refinement of it. Without this cap, "prefer the settlement" would swap
#: one wrong-city bug for another by jumping to a distant namesake.
_NOMINATIM_REFINEMENT_KM = 50.0


def _nominatim_kind(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    return str(result.get("addresstype") or result.get("type") or "").strip().lower()


def _rough_distance_km(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Cheap planar distance; adequate for a 50 km locality check."""
    mean_lat = math.radians((lat_a + lat_b) / 2)
    dy = (lat_a - lat_b) * 111.0
    dx = (lon_a - lon_b) * 111.0 * math.cos(mean_lat)
    return math.hypot(dx, dy)


def _pick_nominatim_result(results: list) -> dict:
    """Choose the Nominatim candidate that best identifies the destination.

    Prefers a settlement point over an administrative area, because only the
    former is guaranteed to sit on the place itself: 平遥县 resolved to its county
    centroid ~11 km from 平遥古城, outside the +/-0.045deg search box, so the
    destination came back with zero POIs even though OSM maps the old town.

    The refinement is bounded by `_NOMINATIM_REFINEMENT_KM`, so it can only move
    a result within its own locality. Falls back to the first result, which is
    Nominatim's own relevance ordering.
    """
    fallback = next((item for item in results if isinstance(item, dict)), None)
    if fallback is None:
        return results[0]

    try:
        anchor_lat, anchor_lon = float(fallback["lat"]), float(fallback["lon"])
    except (KeyError, TypeError, ValueError):
        return fallback

    for result in results:
        result_class = (
            str(result.get("class") or "").strip().lower() if isinstance(result, dict) else ""
        )
        is_settlement = (
            result_class == "place" or _nominatim_kind(result) in _NOMINATIM_SETTLEMENT_TYPES
        )
        if not is_settlement:
            continue
        try:
            lat, lon = float(result["lat"]), float(result["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if _rough_distance_km(lat, lon, anchor_lat, anchor_lon) <= _NOMINATIM_REFINEMENT_KM:
            return result
    return fallback


#: Default order: Open-Meteo first because it is keyless, fast and returns the
#: IANA timezone the forecast endpoint needs; Nominatim as a fallback because it
#: additionally supplies a real administrative bounding box.
DEFAULT_SOURCES: tuple[str, ...] = (SOURCE_OPEN_METEO, SOURCE_NOMINATIM)

KNOWN_SOURCES: frozenset[str] = frozenset({SOURCE_OPEN_METEO, SOURCE_NOMINATIM})


class GeoResolver:
    """Resolves destination names to coordinates with an in-process TTL cache."""

    def __init__(
        self,
        http: FreeHttpClient,
        *,
        language: str = "zh",
        bbox_radius_degrees: float = 0.045,
        cache_ttl_seconds: float = 24 * 3600,
        nominatim_fallback_enabled: bool = True,
        sources: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.http = http
        self.language = language
        self.bbox_radius_degrees = bbox_radius_degrees
        self.cache_ttl_seconds = cache_ttl_seconds
        self.nominatim_fallback_enabled = nominatim_fallback_enabled
        if sources is not None:
            # Explicit order, used to exercise a single source directly (tests,
            # or an operator who wants to pin one provider). Validated eagerly so
            # a typo fails at construction rather than being swallowed by the
            # per-source error handling and surfacing as "geocoding failed".
            resolved = tuple(sources)
            unknown = [name for name in resolved if name not in KNOWN_SOURCES]
            if unknown:
                raise ValueError(
                    f"unknown geocoding source(s): {', '.join(unknown)}; "
                    f"known sources are {', '.join(sorted(KNOWN_SOURCES))}"
                )
            if not resolved:
                raise ValueError("at least one geocoding source is required")
            self.sources = resolved
        elif nominatim_fallback_enabled:
            self.sources = DEFAULT_SOURCES
        else:
            self.sources = (SOURCE_OPEN_METEO,)
        self._cache: dict[str, tuple[float, GeoPlace]] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def _resolver_for(self, source: str):
        if source == SOURCE_OPEN_METEO:
            return self._resolve_open_meteo
        if source == SOURCE_NOMINATIM:
            return self._resolve_nominatim
        raise ValueError(f"unknown geocoding source: {source}")

    async def resolve(self, destination: str) -> GeoPlace:
        key = destination.strip().casefold()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]

        # Transient network failures are already retried inside FreeHttpClient, so
        # a failure here means the source is genuinely down and the next one is
        # tried. Geocoding gates every other free provider, so having a fallback
        # source matters more than knowing which one failed.
        #
        # A candidate is also re-tried when it is only *provisional*, not merely
        # when a source fails. Sources fail loudly; they also succeed while being
        # wrong, and a wrong-but-plausible coordinate is the more dangerous case:
        # it propagates into every POI, route and weather call and the user sees a
        # coherent plan for a city they never asked for. Searching "丽江"
        # (Yunnan) returned a single PPL named 丽江 in Hunan, 1,154 km away, and
        # because that is a non-empty answer the fallback never ran.
        place: GeoPlace | None = None
        provisional: GeoPlace | None = None
        for source in self.sources:
            candidate = await self._try_source(self._resolver_for(source), destination, source)
            if candidate is None:
                continue
            if candidate.source_reference.confidence >= _MINIMUM_TRUSTED_CONFIDENCE:
                place = candidate
                break
            if provisional is None:
                provisional = candidate
            logger.debug(
                "geocoding via %s was provisional for %r (confidence %.2f); "
                "consulting the next source",
                source,
                destination,
                candidate.source_reference.confidence,
            )

        # Fall back to the provisional answer only if nothing better was found:
        # an imperfect coordinate still beats refusing to plan at all.
        place = place or provisional
        if place is None:
            raise GeoResolutionError(
                f"无法解析目的地坐标: {destination} (已尝试: {', '.join(self.sources)})"
            )

        self._cache[key] = (time.monotonic() + self.cache_ttl_seconds, place)
        return place

    async def _try_source(self, resolver, destination: str, label: str) -> GeoPlace | None:
        try:
            return await resolver(destination)
        except Exception as exc:  # noqa: BLE001 - any failure means "try the next source"
            logger.warning("geocoding via %s failed for %r: %s", label, destination, exc)
            return None

    # -- sources -----------------------------------------------------------
    async def _resolve_open_meteo(self, destination: str) -> GeoPlace | None:
        payload = await self.http.get_json(
            OPEN_METEO_GEOCODE_URL,
            params={
                "name": destination,
                "count": 5,
                "language": self.language,
                "format": "json",
            },
        )
        results = (payload or {}).get("results") or [] if isinstance(payload, dict) else []
        if not results:
            return None
        best = max(results, key=_score_open_meteo_result)
        rank, population = _score_open_meteo_result(best)
        latitude = float(best["latitude"])
        longitude = float(best["longitude"])
        return GeoPlace(
            name=str(best.get("name") or destination),
            city=str(best.get("name") or destination),
            country=best.get("country"),
            country_code=_normalize_country_code(best.get("country_code")),
            latitude=latitude,
            longitude=longitude,
            timezone=best.get("timezone"),
            bbox=_clamp_bbox(latitude, longitude, self.bbox_radius_degrees),
            source_reference=self._source_reference(
                source="open-meteo-geocoding",
                source_id=str(best.get("id", destination)),
                # A bare populated place with no population figure is exactly how
                # this endpoint reports an obscure namesake. Keep it, but flag it
                # as provisional so a more precise source gets a chance.
                confidence=(
                    _CONFIDENT_GEOCODE
                    if rank >= _feature_rank(_PPLA) and population > 0
                    else _PROVISIONAL_GEOCODE
                ),
                request_context={
                    "destination": destination,
                    "admin1": best.get("admin1"),
                    "feature_code": best.get("feature_code"),
                    "attribution": OPEN_METEO_ATTRIBUTION,
                },
            ),
        )

    async def _resolve_nominatim(self, destination: str) -> GeoPlace | None:
        payload = await self.http.get_json(
            NOMINATIM_SEARCH_URL,
            params={
                "q": destination,
                "format": "jsonv2",
                "limit": _NOMINATIM_LIMIT,
                "addressdetails": 1,
            },
        )
        if not isinstance(payload, list) or not payload:
            return None
        best = _pick_nominatim_result(payload)
        latitude = float(best["lat"])
        longitude = float(best["lon"])

        # Nominatim returns an administrative bbox that is far too large for a
        # single Overpass query (Chengdu spans ~1.3 degrees of latitude), so it
        # is clamped around the resolved point.
        bbox = _clamp_bbox(latitude, longitude, self.bbox_radius_degrees)
        raw_bbox = best.get("boundingbox")
        if raw_bbox and len(raw_bbox) == 4:
            south, north, west, east = (float(value) for value in raw_bbox)
            bbox = (
                max(latitude - self.bbox_radius_degrees, south),
                max(longitude - self.bbox_radius_degrees, west),
                min(latitude + self.bbox_radius_degrees, north),
                min(longitude + self.bbox_radius_degrees, east),
            )

        address = best.get("address") or {}
        city = address.get("city") or address.get("town") or address.get("county")
        return GeoPlace(
            name=str(best.get("name") or destination),
            city=str(city or best.get("name") or destination),
            country=address.get("country"),
            country_code=_normalize_country_code(address.get("country_code")),
            latitude=latitude,
            longitude=longitude,
            timezone=None,
            bbox=bbox,
            source_reference=self._source_reference(
                source="nominatim",
                source_id=str(best.get("osm_id", destination)),
                confidence=_CONFIDENT_GEOCODE,
                request_context={
                    "destination": destination,
                    "display_name": best.get("display_name"),
                    "result_class": best.get("class"),
                    "result_type": best.get("type"),
                    "attribution": OSM_ATTRIBUTION,
                },
            ),
        )

    def _source_reference(
        self, *, source: str, source_id: str, request_context: dict, confidence: float = 0.85
    ) -> SourceReference:
        now = datetime.now(timezone.utc)
        return SourceReference(
            source=source,
            source_type=SourceType.OPEN_DATA,
            source_id=source_id,
            observed_at=now,
            fetched_at=now,
            # Administrative boundaries change on the order of months.
            expires_at=now + timedelta(days=30),
            request_context=request_context,
            data_version="osm-live",
            confidence=confidence,
            domain=DataDomain.GEO_FACT,
        )


def _normalize_country_code(value) -> str | None:
    """Normalise an ISO 3166-1 alpha-2 code to upper case.

    Open-Meteo returns ``CN`` while Nominatim returns ``cn``; consumers keyed by
    country need one form. Anything that is not a 2-letter code is dropped rather
    than passed on, so a malformed code cannot silently address the wrong country.
    """
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) != 2 or not code.isalpha():
        return None
    return code
