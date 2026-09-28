"""Locating places named in the traveller's own material, using 高德.

Why this exists when the free resolver already does it
------------------------------------------------------
高德 is the best source in this program for **Chinese** place names, and that is not a
preference. Its gazetteer is the one Chinese map data is actually built on, so it knows
灵隐寺, 宽窄巷子 and 文和友 as places; Open-Meteo's gazetteer is a global populated-places list
that treats them as nothing at all, and Nominatim's Chinese coverage comes from volunteer
mapping. Asking a keyed deployment to use the weaker source for the names its users actually
write would be a strange thing to do.

Two things it does that the free sources cannot:

* **``city`` scoping.** 高德 accepts a city name or adcode and searches inside it, so
  人民公园 is looked up in 成都 rather than nationwide. The free resolver approximates this
  with a bounding box and then verifies by distance; this scopes at the source, and the
  distance check is still applied afterwards because a source scoping parameter is a request,
  not a guarantee.
* **``level``.** 高德 says what kind of thing it matched — 兴趣点, 门牌号, 区县 — which is the
  only evidence available here about whether a name resolved to a landmark or to the
  administrative area containing it. The free sources state nothing comparable, which is why
  their citations carry no confidence for Open-Meteo at all.

Terms, and what they cost
-------------------------
高德's terms forbid storing results, so nothing is cached here — the same rule the destination
geocoder already follows, and the reason ``cache_ttl_seconds`` is not offered. Each lookup is
a live request against the owner's own key and quota, which is why the aggregation calls this
capability only when there are names to resolve.

What is *not* done: a result is never turned into an :class:`Attraction`. It stays a
:class:`ResolvedPlace` — no rating, no ticket price, no opening hours, not scheduled into a
day. The only thing 高德 contributes here is a coordinate and a name.
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
from travel_api.providers.amap.client import AMAP_PROVIDER_ID, AmapClient, AmapError
from travel_api.providers.amap.geocode import AmapGeocoder, AmapPlace
from travel_api.providers.place_matching import (
    MAX_DISTANCE_FROM_DESTINATION_KM,
    PRECISION_AREA,
    PRECISION_SPECIFIC,
    PlaceCandidate,
    decide,
    not_attempted,
)

logger = logging.getLogger(__name__)

#: Registry name for this provider. A fourth distinct name rather than sharing
#: ``amap-places`` with the POI provider: the registry is keyed by name, so two providers
#: sharing one would silently drop whichever registered second — the exact bug that once made
#: a keyed deployment plan keyless while the capability matrix went on advertising coverage.
AMAP_PLACE_RESOLVER_NAME = "amap-geocoding"

#: AMap ``level`` values, mapped to the precision bands ``place_matching`` ranks by.
#:
#: A *precision* rather than a confidence, and the distinction is load-bearing. 高德 can be
#: entirely certain that it found 西湖区 — that is a confidence of 1.0 on a match that is the
#: wrong kind of thing. What the caller needs to know is whether the answer is the landmark or
#: the area around it, so the bands say that and ``amap_confidence`` separately says how sure
#: the source is.
#:
#: An unrecognised level gets no band rather than a downgrade: 高德 adds levels over time, and
#: one being new is not evidence that the match is bad.
_SPECIFIC_LEVELS = ("兴趣点", "门牌号", "村庄", "风景名胜")
_COARSE_LEVELS = ("区县", "城市", "省", "国家")

#: ``level`` values whose match is certain but coarse — a province or a city is exactly what
#: AMap says it is, so the match confidence is high while the precision is low.
_ADMIN_LEVELS = ("省", "城市", "区县", "国家")


def amap_precision(level: str | None) -> float | None:
    """The precision band for an AMap ``level``, or ``None`` when the level is not recognised."""
    if not level:
        return None
    if any(marker in level for marker in _SPECIFIC_LEVELS):
        return PRECISION_SPECIFIC
    if any(marker in level for marker in _COARSE_LEVELS):
        return PRECISION_AREA
    return None


def amap_confidence(level: str | None) -> float | None:
    """How sure 高德 appears to be about a match, from its ``level``.

    ``None`` rather than a default: "AMap did not say what it matched" is a real state, and
    filling it in with a plausible number is how an unearned figure gets into a citation.
    """
    if not level:
        return None
    # An administrative level is a confident answer to a different question. AMap is not
    # uncertain that 西湖区 is 西湖区, so the match confidence is high even though the
    # precision is low — reporting it as unsure would misdescribe what happened.
    if any(marker in level for marker in _ADMIN_LEVELS):
        return 0.85
    if any(marker in level for marker in _SPECIFIC_LEVELS):
        return 0.85
    return None


class AmapPlaceResolver:
    """Locates names with 高德's geocoder, scoped to the destination's city."""

    def __init__(
        self,
        client: AmapClient,
        *,
        geocoder: AmapGeocoder,
        name: str = AMAP_PLACE_RESOLVER_NAME,
        max_distance_km: float = MAX_DISTANCE_FROM_DESTINATION_KM,
    ) -> None:
        self.name = name
        self.client = client
        #: Shared with the weather provider, which needs an adcode. Passing one instance means
        #: a trip resolves its city once rather than once per capability.
        self.geocoder = geocoder
        self.max_distance_km = max_distance_km
        self.last_city: str | None = None

    async def resolve_places(
        self,
        names: list[str],
        profile: TripProfile,
    ) -> ProviderResult:
        """Locate each name inside the trip's own city.

        The city is taken from ``profile.destination``'s own 高德 lookup, because the
        destination is the only context that makes a bare name unambiguous and it is always
        available. 高德's scoping parameter takes a city rather than a point, so the query is
        scoped by city name and then verified by distance against the same lookup's
        coordinate.
        """
        if not names:
            return ProviderResult(status="success", provider=self.name, data=[])

        destination = await self._destination(profile)
        if destination is None:
            # Without the destination there is nothing to scope by and nothing to check
            # against, so no lookup could locate anything meaningfully. Reported as
            # NOT_ATTEMPTED rather than NOT_FOUND: no lookup was possible, and
            # 「没有找到这个地方」would blame the place for a failure to locate the trip — a
            # claim nobody made.
            return ProviderResult(
                status="degraded",
                provider=self.name,
                data=not_attempted(
                    names,
                    "高德没有解析出目的地，因此没有对笔记里的地名做定位；"
                    "没有目的地就无法判断同名地点中哪一个才是笔记所指的",
                ),
                errors=[f"高德无法解析目的地：{profile.destination}"],
            )

        city = destination.city_name
        self.last_city = city
        centre = Coordinates(latitude=destination.latitude, longitude=destination.longitude)

        places: list[ResolvedPlace] = []
        errors: list[str] = []

        for raw_name in names:
            # Used exactly as the extractor produced it — see ``place_matching``'s docstring for
            # why there is no tidying step here.
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
                candidates = await self._candidates(name, city)
            except AmapError as exc:
                # Reported per name, not raised: one unusable name must not discard the
                # results for every other name in the same note. The message is kept because
                # 高德 distinguishes an invalid key from a quota exhaustion, and those two
                # need different actions from the person reading it.
                logger.warning("AMap place lookup failed for %r: %s", name, exc)
                errors.append(f"{name}: {exc}")
                places.append(
                    ResolvedPlace(
                        name=name,
                        status=PlaceResolutionStatus.NOT_FOUND,
                        reason=f"高德查询失败：{exc}",
                    )
                )
                continue
            places.append(
                decide(
                    name,
                    candidates,
                    destination=centre,
                    source_type=SourceType.OFFICIAL_API,
                    max_distance_km=self.max_distance_km,
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

    async def _destination(self, profile: TripProfile) -> AmapPlace | None:
        """The trip's own place from 高德: city *and* coordinate, from one lookup.

        One call rather than two because the two are the same answer. ``city`` scopes the
        per-name queries and the coordinate verifies them, and splitting them would mean
        geocoding the destination twice and — worse — letting the scoped city and the checked
        coordinate come from two different responses, which nothing in the result would show.
        """
        try:
            return await self.geocoder.resolve(profile.destination)
        except AmapError as exc:
            logger.warning("AMap could not geocode %r: %s", profile.destination, exc)
            return None

    async def _candidates(self, name: str, city: str | None) -> list[PlaceCandidate]:
        """Candidates from 高德, converted to the neutral shape the shared decision reads.

        The ``AmapPlace`` list is kept whole rather than pre-ranked by 高德's own
        administrative preference. ``_rank`` exists for resolving a *trip destination*, where
        the city beats a shop named after the city; for a place named in a note that ordering
        is backwards — 灵隐寺 is a 风景名胜, and preferring a same-named 区县 over it would
        choose the district instead of the temple. Distance from the destination is the only
        ordering that fits both, and it is applied once, in ``place_matching``.
        """
        results: list[AmapPlace] = await self.geocoder.candidates(
            name, city=city, raise_on_error=True
        )
        return [
            PlaceCandidate(
                source=AMAP_PROVIDER_ID,
                source_id=f"{place.adcode or ''}:{place.formatted_address}",
                latitude=place.latitude,
                longitude=place.longitude,
                confidence=amap_confidence(place.level),
                precision=amap_precision(place.level),
                display_name=place.formatted_address or None,
                detail={
                    "level": place.level,
                    "city": place.city,
                    "district": place.district,
                    "adcode": place.adcode,
                    "scoped_to_city": city,
                },
            )
            for place in results
        ]


__all__ = [
    "AMAP_PLACE_RESOLVER_NAME",
    "AmapPlaceResolver",
    "amap_confidence",
    "amap_precision",
]
