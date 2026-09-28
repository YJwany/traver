"""Locating places that the traveller's own material named, with no API key.

The problem this solves is narrow and specific. ``_build_ingested_notes`` matches a note's
text against entities that were already aggregated, so a note about 灵隐寺 attaches to the
灵隐寺 that Overpass found. A note naming somewhere Overpass did *not* find has nowhere to
attach: it is reported as an unmatched mention and the plan cannot use it. That is the common
case, not the exception — a 小红书 post names restaurants, cafés and viewpoints a POI query
never returned, and 「你提供的越多，安排得越详细」 is exactly the promise this fails.

So: geocode the names. The decision about what counts as a match — nearest to the
destination, reported as 在别处 when it is too far, distinguished from "nobody was asked" —
lives in :mod:`travel_api.providers.place_matching`, once, so this source and 高德 cannot
drift apart on it. What is here is only the part that differs: how to ask.

Two free sources, and they are asked in a specific order for a specific reason:

* **Open-Meteo's geocoder** has no scoping parameter. It answers from a global gazetteer, so
  every answer it gives is a candidate rather than a result — which is fine, because the
  distance check downstream is what decides. It is tried first because it is the geocoder this
  deployment already uses for the destination itself, so it is known reachable.
* **Nominatim** is asked when Open-Meteo has nothing *or* when everything it returned is out
  of area. Those are different failures and only one of them is visible as an empty list:
  Open-Meteo happily answers 人民公园 with a park in a different city, and that answer looks
  like success. Nominatim's ``bounded=1`` is the part that matters — without it ``viewbox``
  only biases the ranking and a globally more prominent namesake still wins, which is the
  failure this exists to prevent. With it, Nominatim returns nothing rather than something
  wrong, and "returned nothing" is a result the caller can report honestly.

Nothing here is merged into a measured POI: see :class:`ResolvedPlace`, a different type from
:class:`Attraction` precisely so that cannot happen by accident. And no travel time is derived
from a resolved place. The straight-line distance to the destination centre is reported
because it is a property of two coordinates and is labelled as straight-line; how long it
takes to get there needs a routing service, which is a different capability.
"""

from __future__ import annotations

import logging

from travel_api.domain.models import (
    Coordinates,
    PlaceResolutionStatus,
    ProviderResult,
    ResolvedPlace,
    SourceType,
    TripProfile,
)
from travel_api.providers.free.geo import (
    NOMINATIM_SEARCH_URL,
    OPEN_METEO_GEOCODE_URL,
    FreeHttpClient,
    GeoResolver,
)
from travel_api.providers.place_matching import (
    MAX_DISTANCE_FROM_DESTINATION_KM,
    PRECISION_AREA,
    PRECISION_SPECIFIC,
    PRECISION_VENUE,
    RESULT_LIMIT,
    PlaceCandidate,
    any_within,
    decide,
    not_attempted,
)

logger = logging.getLogger(__name__)

#: Provider name. Distinct from ``openstreetmap`` (the POI provider) on purpose: the registry
#: keys providers by name, so two providers sharing one name means one is silently discarded —
#: which happened before, with three providers all called ``amap``. It keeps the
#: ``openstreetmap`` prefix because it is the same family of sources, and the shortest name
#: that is still unique is the one to use.
FREE_PLACE_RESOLVER_NAME = "openstreetmap-places"

#: Source identifiers written into citations. These are the same strings the destination
#: geocoder uses, so a reader who sees ``nominatim`` on a trip's own coordinate and on a
#: located mention knows it is the same service.
SOURCE_OPEN_METEO = "open-meteo-geocoding"
SOURCE_NOMINATIM = "nominatim"

#: Nominatim result types, by how specific they are. A landmark or a building is a real answer
#: for a named place; an administrative boundary is a different kind of thing that happens to
#: share the name. Coarse bands rather than decimals, because a decimal would imply a
#: calibration this does not have — the same reasoning as ``ExtractionConfidence``.
_NOMINATIM_HIGH_CONFIDENCE_TYPES = frozenset(
    {
        "attraction",
        "museum",
        "viewpoint",
        "park",
        "garden",
        "theme_park",
        "zoo",
        "artwork",
        "monument",
        "memorial",
    }
)
_NOMINATIM_MEDIUM_CONFIDENCE_TYPES = frozenset(
    {
        "restaurant",
        "cafe",
        "hotel",
        "guest_house",
        "hostel",
        "mall",
        "marketplace",
        "place_of_worship",
        "temple",
        "monastery",
        "castle",
        "ruins",
        "peak",
        "lake",
        "island",
        "beach",
        "station",
        "aerodrome",
        "bus_station",
        "ferry_terminal",
    }
)

#: Nominatim types that are a real answer to "where is this name" and a coarse one. Separate
#: from the confidence sets above because these are where ``precision`` and ``confidence``
#: genuinely diverge: Nominatim is entirely sure it found 西湖区.
_NOMINATIM_AREA_TYPES = frozenset(
    {
        "administrative",
        "city",
        "town",
        "village",
        "hamlet",
        "municipality",
        "county",
        "state",
        "province",
        "region",
        "country",
        "neighbourhood",
        "suburb",
        "quarter",
        "city_block",
        "borough",
    }
)


def nominatim_confidence(result_type: object) -> float | None:
    """A band for a Nominatim result's specificity, or ``None`` when the type is not known.

    ``None`` propagates to the citation rather than becoming a default, because "Nominatim did
    not tell us the type" and "Nominatim said it is a restaurant" are different amounts of
    information and only one of them is evidence.
    """
    if not isinstance(result_type, str):
        return None
    if result_type in _NOMINATIM_HIGH_CONFIDENCE_TYPES:
        return 0.75
    if result_type in _NOMINATIM_MEDIUM_CONFIDENCE_TYPES:
        return 0.6
    return None


def nominatim_precision(result_type: object) -> float | None:
    """How precise a Nominatim result is, on the bands ``place_matching`` ranks by.

    Separate from :func:`nominatim_confidence` because they answer different questions. A
    ``park`` and a ``restaurant`` are both things Nominatim is confident it found, and one is a
    landmark while the other is a named business. Ranking candidates needs the second fact;
    ranking them by the first would treat a café and a scenic area as equivalent.
    """
    if not isinstance(result_type, str):
        return None
    if result_type in _NOMINATIM_HIGH_CONFIDENCE_TYPES:
        return PRECISION_SPECIFIC
    if result_type in _NOMINATIM_MEDIUM_CONFIDENCE_TYPES:
        return PRECISION_VENUE
    if result_type in _NOMINATIM_AREA_TYPES:
        return PRECISION_AREA
    return None


def open_meteo_precision(feature_code: object) -> float | None:
    """How precise an Open-Meteo result is — always an area, never a landmark.

    The geocoding API's ``feature_code`` values are all populated-place classes (``PPL``,
    ``PPLA``, ``PPLC``, ``PPLX`` …), which is what makes this a constant rather than a lookup
    table: Open-Meteo's gazetteer contains *places people live*, not attractions, restaurants or
    buildings. So a 西湖 from this source is the town named 西湖 if one exists, never the lake —
    a fact worth stating, because it is exactly why precision matters when Nominatim's answer is
    available alongside it.

    ``None`` for a record with no feature code, because that is a shape this does not understand
    and it should not be credited with a precision it may not have.
    """
    if not isinstance(feature_code, str) or not feature_code:
        return None
    return PRECISION_AREA


class FreePlaceResolver:
    """Locates names with Open-Meteo's geocoder, falling back to Nominatim.

    Both are already used for the destination itself, so their behaviour, rate limits and
    attribution obligations are known to this project rather than assumed. Neither needs a key,
    which matters: the whole point of the feature is that a note the traveller pasted becomes
    useful with no further setup.
    """

    def __init__(
        self,
        http: FreeHttpClient,
        *,
        geo_resolver: GeoResolver | None = None,
        language: str = "zh",
        timeout_seconds: float = 90.0,
        max_distance_km: float = MAX_DISTANCE_FROM_DESTINATION_KM,
        nominatim_fallback: bool = True,
        name: str = FREE_PLACE_RESOLVER_NAME,
    ) -> None:
        self.name = name
        self.http = http
        #: Shared with the rest of the free stack. Reusing it is not an optimisation for its
        #: own sake: it means the destination is geocoded **once** per process, so a plan
        #: cannot scope its place lookups to one coordinate while its POIs came from another.
        #: Constructed here when absent so the class is usable on its own in a test.
        self.geo_resolver = geo_resolver or GeoResolver(
            http, language=language, nominatim_fallback_enabled=nominatim_fallback
        )
        self.language = language
        self.timeout_seconds = timeout_seconds
        self.max_distance_km = max_distance_km
        self.nominatim_fallback = nominatim_fallback
        #: Set by ``resolve_places`` so a caller — and a test — can see which sources were
        #: actually contacted, rather than having to infer it from the citations.
        self.last_sources: list[str] = []

    async def resolve_places(
        self,
        names: list[str],
        profile: TripProfile,
    ) -> ProviderResult:
        """Locate each name near the trip's own destination, reporting an outcome for every one.

        Every input name produces exactly one :class:`ResolvedPlace`, in the order given.
        That is a contract, not a convenience: a caller matching results back to the mentions
        it asked about cannot do so if the list is shorter than the request, and the omissions
        would be exactly the failures — the ones worth reporting.
        """
        self.last_sources = []
        if not names:
            return ProviderResult(status="success", provider=self.name, data=[])

        centre = await self._trip_centre(profile)
        if centre is None:
            # The destination itself could not be geocoded, so there is no coordinate for a
            # name to be near and no way to tell a same-named place here from one 1,000 km
            # away. Reported as NOT_ATTEMPTED rather than NOT_FOUND: no lookup was possible,
            # and saying「没有找到这个地方」would blame the place for a failure of the trip's
            # own centre — a claim nobody made.
            return ProviderResult(
                status="degraded",
                provider=self.name,
                data=not_attempted(
                    names,
                    "无法确定目的地的位置，因此没有对笔记里的地名做定位；"
                    "没有位置就无法判断同名地点中哪一个才是笔记所指的",
                ),
                errors=[f"无法解析目的地的坐标：{profile.destination}"],
            )

        places: list[ResolvedPlace] = []
        errors: list[str] = []

        for raw_name in names:
            # The name is used exactly as the extractor produced it. There is no tidying step
            # on purpose — see the module docstring in ``place_matching``: ``_PLACE_PATTERN``
            # already stops at the first place suffix, so 「推荐去灵隐寺看看」 arrives here as
            # 灵隐寺 and a downstream trimmer would be fixing nothing while adding a second,
            # disagreeing rule about what a place name is.
            name = raw_name.strip()
            if not name:
                places.append(
                    ResolvedPlace(
                        name=raw_name,
                        status=PlaceResolutionStatus.NOT_ATTEMPTED,
                        reason="提取到的内容不是一个可用于查询的地名",
                    )
                )
                continue
            try:
                places.append(await self._resolve_one(name, centre))
            except Exception as exc:  # noqa: BLE001 - one bad name must not lose the rest
                # A single failed lookup degrades that one entry rather than the whole call.
                # Letting it raise would make one unlocatable name discard the results for
                # every other name in the same note.
                logger.warning("place resolution failed for %r: %s", name, exc)
                errors.append(f"{name}: {exc}")
                places.append(
                    ResolvedPlace(
                        name=name,
                        status=PlaceResolutionStatus.NOT_FOUND,
                        reason=f"查询失败：{exc}",
                    )
                )

        references = [
            place.source_reference for place in places if place.source_reference is not None
        ]
        return ProviderResult(
            status="degraded" if errors else "success",
            provider=self.name,
            data=places,
            source_references=references,
            errors=errors,
        )

    async def _trip_centre(self, profile: TripProfile) -> Coordinates | None:
        """Where the trip is, as a coordinate, from the cached destination lookup.

        Returns ``None`` rather than raising when the destination cannot be resolved. The
        caller turns that into a stated reason for every name, which is more useful than an
        exception: the plan still builds, and the reader learns *why* the mentions are not on
        the map.
        """
        try:
            place = await self.geo_resolver.resolve(profile.destination)
        except Exception as exc:  # noqa: BLE001 - a missing centre is a reported outcome
            logger.warning(
                "could not geocode the trip destination %r for place lookups: %s",
                profile.destination,
                exc,
            )
            return None
        return Coordinates(latitude=place.latitude, longitude=place.longitude)

    # -- one name ----------------------------------------------------------

    async def _resolve_one(self, name: str, destination: Coordinates) -> ResolvedPlace:
        open_meteo = await self._query_open_meteo(name)
        if self.nominatim_fallback and not any_within(
            open_meteo, destination, self.max_distance_km
        ):
            candidates = [*open_meteo, *await self._query_nominatim(name, destination)]
        else:
            candidates = open_meteo
        return decide(
            name,
            candidates,
            destination=destination,
            source_type=SourceType.OPEN_DATA,
            max_distance_km=self.max_distance_km,
        )

    # -- sources -----------------------------------------------------------

    async def _query_open_meteo(self, name: str) -> list[PlaceCandidate]:
        """Candidates from Open-Meteo's geocoder, which has no scoping parameter.

        Every result is therefore a global match, which is why the distance check downstream
        is not optional for this source.
        """
        try:
            payload = await self.http.get_json(
                OPEN_METEO_GEOCODE_URL,
                params={
                    "name": name,
                    "count": RESULT_LIMIT,
                    "language": self.language,
                    "format": "json",
                },
            )
        except Exception as exc:  # noqa: BLE001 - the caller tries the next source
            logger.warning("open-meteo geocoding failed for %r: %s", name, exc)
            return []
        self.last_sources.append(SOURCE_OPEN_METEO)

        results = (payload or {}).get("results") or [] if isinstance(payload, dict) else []
        out: list[PlaceCandidate] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                latitude = float(item["latitude"])
                longitude = float(item["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(
                PlaceCandidate(
                    source=SOURCE_OPEN_METEO,
                    source_id=str(item.get("id", name)),
                    latitude=latitude,
                    longitude=longitude,
                    # Open-Meteo states no match confidence. Reporting None is right;
                    # inventing one from the feature code would be a number that looks
                    # measured and is not.
                    confidence=None,
                    # Its precision is a constant, and a low one: this gazetteer holds
                    # populated places, so it can never return the landmark a note meant.
                    # Stated rather than left None so a nearby Nominatim landmark outranks it.
                    precision=open_meteo_precision(item.get("feature_code")),
                    display_name=str(item.get("name") or "") or None,
                    detail={
                        "admin1": item.get("admin1"),
                        "country_code": item.get("country_code"),
                        "feature_code": item.get("feature_code"),
                    },
                )
            )
        return out

    async def _query_nominatim(self, name: str, destination: Coordinates) -> list[PlaceCandidate]:
        """Candidates from Nominatim, **scoped** to a box around the destination.

        ``bounded=1`` is the part that matters: without it ``viewbox`` only biases the ranking
        and a globally more prominent namesake still wins, which is the failure this is here
        to prevent. With it, Nominatim returns nothing rather than something wrong — and
        "returned nothing" is a result the caller can report honestly.
        """
        window = max(self.max_distance_km, 1.0) / 111.0  # km to degrees of latitude
        west = destination.longitude - window
        east = destination.longitude + window
        south = destination.latitude - window
        north = destination.latitude + window
        try:
            payload = await self.http.get_json(
                NOMINATIM_SEARCH_URL,
                params={
                    "q": name,
                    "format": "jsonv2",
                    "limit": RESULT_LIMIT,
                    "addressdetails": 0,
                    # Nominatim requires left,top,right,bottom — i.e. west,north,east,south.
                    # Getting this order wrong produces a box on the other side of the
                    # antimeridian, which returns nothing and looks like "place not found".
                    "viewbox": f"{west},{north},{east},{south}",
                    "bounded": 1,
                },
            )
        except Exception as exc:  # noqa: BLE001 - the caller reports "no candidates"
            logger.warning("nominatim place lookup failed for %r: %s", name, exc)
            return []
        self.last_sources.append(SOURCE_NOMINATIM)

        if not isinstance(payload, list):
            return []
        out: list[PlaceCandidate] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                latitude = float(item["lat"])
                longitude = float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(
                PlaceCandidate(
                    source=SOURCE_NOMINATIM,
                    source_id=str(item.get("osm_id", name)),
                    latitude=latitude,
                    longitude=longitude,
                    confidence=nominatim_confidence(item.get("type")),
                    precision=nominatim_precision(item.get("type")),
                    display_name=str(item.get("name") or item.get("display_name") or "") or None,
                    detail={"result_type": item.get("type"), "result_class": item.get("class")},
                )
            )
        return out


__all__ = [
    "FREE_PLACE_RESOLVER_NAME",
    "SOURCE_NOMINATIM",
    "SOURCE_OPEN_METEO",
    "FreePlaceResolver",
    "nominatim_confidence",
    "nominatim_precision",
    "open_meteo_precision",
]
