"""AMap geocoding: a destination name to coordinates, city and adcode.

Two AMap endpoints, and the difference matters
----------------------------------------------
* ``/v3/geocode/geo`` — structured address lookup, returning ``province``/``city``/
  ``district``/``adcode``/``location`` plus a ``level`` describing how specific the
  match is (省/市/区/…).
* ``/v3/geocode/regeo`` — coordinates back to an address.

``level`` is what makes this useful as a *validator*, not just a lookup. Asking for
北京 can match at province, city or district granularity, and the same query for a
typo can match at 兴趣点 level somewhere unrelated. The level is therefore carried on
the result so a caller can reject a match that is too coarse or too specific for the
trip it is planning.

Coordinates are swapped exactly once
------------------------------------
AMap sends ``"lng,lat"`` and returns a latitude-first pair from the shared parser. It
is the only conversion, and it is tested against a real Beijing coordinate, because a
swap is numerically valid and would place the trip in the wrong hemisphere with no
error anywhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from travel_api.providers.amap.client import AmapClient, AmapError, parse_location

logger = logging.getLogger(__name__)

GEOCODE_PATH = "/v3/geocode/geo"
REVERSE_GEOCODE_PATH = "/v3/geocode/regeo"

#: AMap ``level`` values that describe an administrative area, coarsest first. A
#: match on one of these is a city or region; anything else is a street, a POI or a
#: building, which for a trip destination is usually a mismatch.
ADMIN_LEVELS = ("国家", "省", "城市", "区县")


def is_admin_level(level: str | None) -> bool:
    """Whether a ``level`` describes an administrative area.

    Used to tell "北京 as a city" from "some 北京烤鸭 restaurant" — both are valid
    AMap matches for the query 北京, and only one is a trip destination.
    """
    if not level:
        return False
    return any(marker in level for marker in ADMIN_LEVELS)


@dataclass(frozen=True)
class AmapPlace:
    """One geocoding result."""

    formatted_address: str
    province: str | None
    city: str | None
    district: str | None
    adcode: str | None
    latitude: float
    longitude: float
    level: str | None

    @property
    def is_admin(self) -> bool:
        return is_admin_level(self.level)

    @property
    def city_name(self) -> str:
        """The best available city label.

        Falls back through city -> province -> formatted address because AMap leaves
        ``city`` empty for municipalities (北京/上海/天津/重庆 report the name in
        ``province`` with an empty ``city``), which would otherwise make a valid
        match look like it had no city at all.
        """
        return self.city or self.province or self.formatted_address


class AmapGeocoder:
    """Resolves destination names with AMap, preferring administrative matches."""

    def __init__(self, client: AmapClient, *, name: str = "amap", cache_ttl_seconds: int = 0):
        self.name = name
        self.client = client
        #: Left at 0 and unused by default. AMap's terms forbid storing results, so
        #: this class keeps no cache; the parameter exists so a test or a caller with
        #: its own licensing can opt in, and so the default is visibly "no caching".
        self.cache_ttl_seconds = cache_ttl_seconds
        self.last_place: AmapPlace | None = None
        self.last_query: str | None = None

    async def resolve(self, destination: str) -> AmapPlace | None:
        """Geocode a destination, preferring an administrative-area match.

        AMap orders its own results, but for a trip destination the ordering that
        matters is "is this the city or a shop named after it". Candidates are
        therefore ranked so an administrative match wins, and among those the
        coarsest (a city outranks a district) — the same class of bug that once
        ranked Paris, Texas above Paris, France in the free resolver.
        """
        query = (destination or "").strip()
        if not query:
            return None

        try:
            payload = await self.client.get(GEOCODE_PATH, params={"address": query})
        except AmapError as exc:
            logger.warning("AMap geocode failed for %r: %s", query, exc)
            return None

        geocodes = payload.get("geocodes")
        if not isinstance(geocodes, list):
            return None

        candidates: list[AmapPlace] = []
        for item in geocodes:
            if not isinstance(item, dict):
                continue
            coordinates = parse_location(item.get("location"))
            # Without a usable coordinate the record cannot locate anything, so it is
            # skipped rather than defaulted.
            if coordinates is None:
                continue
            latitude, longitude = coordinates
            candidates.append(
                AmapPlace(
                    formatted_address=str(item.get("formatted_address") or ""),
                    province=str(item.get("province") or "").strip() or None,
                    city=str(item.get("city") or "").strip() or None,
                    district=str(item.get("district") or "").strip() or None,
                    adcode=str(item.get("adcode") or "").strip() or None,
                    latitude=latitude,
                    longitude=longitude,
                    level=str(item.get("level") or "").strip() or None,
                )
            )

        if not candidates:
            return None

        best = min(candidates, key=_rank)
        self.last_place = best
        self.last_query = query
        return best

    async def adcode_for(self, destination: str) -> str | None:
        place = await self.resolve(destination)
        return place.adcode if place else None

    async def reverse(self, latitude: float, longitude: float) -> AmapPlace | None:
        """Coordinates to the address AMap reports for them.

        ``addressComponent`` is **camelCase** in AMap's response while every sibling
        field is snake_case — reading it as ``address_component`` yields ``None`` and
        silently loses the city and district.
        """
        try:
            payload = await self.client.get(
                REVERSE_GEOCODE_PATH,
                params={"location": f"{longitude:.6f},{latitude:.6f}", "extensions": "base"},
            )
        except AmapError as exc:
            logger.warning("AMap reverse geocode failed: %s", exc)
            return None

        regeocode = payload.get("regeocode")
        if not isinstance(regeocode, dict):
            return None
        component = regeocode.get("addressComponent")
        if not isinstance(component, dict):
            component = {}
        return AmapPlace(
            formatted_address=str(regeocode.get("formatted_address") or ""),
            province=str(component.get("province") or "").strip() or None,
            city=str(component.get("city") or "").strip() or None,
            district=str(component.get("district") or "").strip() or None,
            adcode=str(component.get("adcode") or "").strip() or None,
            latitude=latitude,
            longitude=longitude,
            level=None,
        )


#: Coarseness rank for administrative levels, most preferred first. Lower sorts first.
_LEVEL_ORDER = ("国家", "省", "城市", "区县")


def _rank(place: AmapPlace) -> tuple[int, int, float]:
    """Sort key: administrative matches first, then the coarsest level.

    The third element is a tie-breaker on coordinate magnitude, present only so the
    ordering is total and therefore deterministic — without it two equally-ranked
    candidates could swap between runs and the trip would move.
    """
    if place.level is None:
        level_rank = len(_LEVEL_ORDER)
    else:
        level_rank = next(
            (index for index, marker in enumerate(_LEVEL_ORDER) if marker in place.level),
            len(_LEVEL_ORDER),
        )
    return (1 if not place.is_admin else 0, level_rank, abs(place.latitude))


__all__ = [
    "ADMIN_LEVELS",
    "GEOCODE_PATH",
    "REVERSE_GEOCODE_PATH",
    "AmapGeocoder",
    "AmapPlace",
    "is_admin_level",
]
