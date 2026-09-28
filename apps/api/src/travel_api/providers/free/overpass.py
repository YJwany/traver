"""OpenStreetMap POI provider backed by the Overpass API.

Scope and honesty boundaries
----------------------------
OpenStreetMap is a *geo fact* source. It knows where a hotel, restaurant or
attraction is, what it is called, and often its opening hours. It does **not**
know prices or availability. Accordingly:

* ``search_hotels`` / ``search_restaurants`` / ``search_attractions`` return real
  entities with ``rating=None`` and ``average_cost=None`` unless OSM actually
  carries a verifiable value. OSM's ``stars`` tag is a hotel *classification*,
  not a guest review score, so it is preserved on the source reference instead
  of being written into ``Hotel.rating``.
* ``search_room_offers`` returns an empty successful result. Inventing a price
  here would fabricate a commercial fact, so the budget engine is left to mark
  the hotel component as an explicit estimate instead.

One union query is issued per destination and cached, so the three capability
methods share a single request against this shared community service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid5

from travel_api.domain.models import (
    Attraction,
    Coordinates,
    DataDomain,
    Hotel,
    Location,
    Money,
    PriceType,
    ProviderHealth,
    ProviderResult,
    Restaurant,
    SourceReference,
    SourceType,
    TripProfile,
)
from travel_api.providers.free.geo import OSM_ATTRIBUTION, GeoResolver
from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.free.opening_hours import parse_opening_hours

logger = logging.getLogger(__name__)

#: Stable namespace so the same OSM element always maps to the same UUID across
#: refreshes, which keeps snapshots and change history meaningful.
OSM_NAMESPACE = UUID("2f8a1d64-6c1b-4f0e-9a37-1d5c8b2e7f31")

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    # ex-overpass.kumi.systems, renamed upstream.
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

HOTEL_TOURISM_VALUES = ("hotel", "hostel", "guest_house", "apartment")
FOOD_AMENITY_VALUES = ("restaurant", "cafe", "fast_food")
ATTRACTION_TOURISM_VALUES = (
    "attraction",
    "museum",
    "viewpoint",
    "gallery",
    "zoo",
    "theme_park",
)

#: Default visit length by OSM type, used when OSM has no explicit duration.
DEFAULT_DURATION_BY_TYPE: dict[str, int] = {
    "museum": 120,
    "gallery": 90,
    "viewpoint": 30,
    "zoo": 180,
    "theme_park": 240,
    "attraction": 90,
}

_NAME_KEYS = ("name:zh", "name", "name:en", "int_name")
_ALIAS_KEYS = (
    "name:en",
    "name:zh",
    "int_name",
    "official_name",
    "alt_name",
    "old_name",
    "loc_name",
)


def osm_stable_id(element_type: str, element_id: int) -> UUID:
    return uuid5(OSM_NAMESPACE, f"osm:{element_type}:{element_id}")


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pick_name(tags: dict) -> str | None:
    for key in _NAME_KEYS:
        name = _clean(tags.get(key))
        if name:
            return name
    return None


def _collect_aliases(tags: dict, canonical: str) -> list[str]:
    aliases: set[str] = set()
    for key in _ALIAS_KEYS:
        value = _clean(tags.get(key))
        if value and value != canonical:
            aliases.add(value)
    # ``alt_name`` may hold several names separated by semicolons.
    for key in ("alt_name", "old_name"):
        raw = _clean(tags.get(key))
        if raw and ";" in raw:
            aliases.update(part.strip() for part in raw.split(";") if part.strip())
    aliases.discard(canonical)
    return sorted(aliases)


def _wiki_link_context(tags: dict) -> dict:
    """Extract OSM's explicit encyclopedia links, if the element has them.

    These tags are the *only* safe route to descriptive text. Matching an
    attraction to an article by name was measured to be wrong often: 火烈鸟
    resolved to the taxonomic family 紅鸛科, and 人民公园 to a disambiguation
    page listing parks in other cities. Both would attach text about something
    else, which is a fabricated fact.

    These tags are written by a human mapper and cover roughly 8% of Chengdu
    attractions, but where present they were correct in every case checked —
    including cases name matching can never get right (``永陵`` links to
    ``zh:王建墓``, ``武侯祠`` to ``zh:成都武侯祠``, sidestepping the
    disambiguation page).
    """
    context: dict = {}
    wikipedia = _clean(tags.get("wikipedia"))
    if wikipedia:
        context["wikipedia"] = wikipedia
    wikidata = _clean(tags.get("wikidata"))
    if wikidata:
        context["wikidata"] = wikidata
    return context


def _parse_decimal(value: object) -> Decimal | None:
    """Parse a numeric OSM value such as ``50``, ``50 CNY`` or ``¥50``.

    A leading sign is honoured, not skipped. Skipping it turned ``-1`` into
    ``1``: a malformed star classification became a valid one, and a negative
    charge became a positive price. Negative results are rejected, so the
    ``amount >= 0`` guard below is reachable rather than dead.
    """
    text = _clean(value)
    if not text:
        return None
    sign = 1
    body = text
    if body[0] in "+-":
        if body[0] == "-":
            sign = -1
        body = body[1:]
    digits = ""
    for char in body:
        if char.isdigit() or char == ".":
            digits += char
        elif digits:
            break
    if not digits:
        return None
    try:
        amount = Decimal(digits) * sign
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


def _parse_star_classification(value: object) -> int | None:
    """Parse OSM's ``stars`` tag into a hotel classification.

    This is a *classification* (how many stars the property is officially rated
    as), not a guest review score. The two are not interchangeable, so the result
    must never be written into ``Hotel.rating``: the decision engine scores
    ``rating / 5`` as a guest rating, so a 5-star classification would be
    indistinguishable from a perfect 5.0/5 guest score.
    """
    stars = _parse_decimal(value)
    if stars is None:
        return None
    if stars != int(stars):  # tolerate "4.0" but reject "4.5"
        return None
    classification = int(stars)
    return classification if 0 <= classification <= 5 else None


@dataclass(frozen=True)
class _Poi:
    """A normalized Overpass element, independent of OSM's node/way split."""

    element_type: str
    element_id: int
    latitude: float
    longitude: float
    tags: dict

    @property
    def identity(self) -> str:
        return f"{self.element_type}/{self.element_id}"


class OverpassPoiProvider:
    """Serves hotel / restaurant / attraction capabilities from OpenStreetMap."""

    def __init__(
        self,
        name: str = "openstreetmap",
        *,
        http: FreeHttpClient,
        geo_resolver: GeoResolver,
        mirrors: list[str] | None = None,
        hotel_limit: int = 40,
        food_limit: int = 60,
        attraction_limit: int = 60,
        query_timeout_seconds: int = 25,
        #: Per-provider call budget. The public endpoints are slow, and the
        #: combined query plus mirror fallback can legitimately take a while.
        timeout_seconds: float = 40.0,
        poi_ttl_seconds: int = 3600,
    ) -> None:
        self.name = name
        self.http = http
        self.geo_resolver = geo_resolver
        self.mirrors = list(mirrors or OVERPASS_MIRRORS)
        self.hotel_limit = hotel_limit
        self.food_limit = food_limit
        self.attraction_limit = attraction_limit
        self.query_timeout_seconds = query_timeout_seconds
        self.timeout_seconds = timeout_seconds
        self.poi_ttl_seconds = poi_ttl_seconds
        self._cache: dict[str, tuple[float, list[_Poi], SourceReference]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.last_endpoint: str | None = None
        self._city_cache: dict[str, str] = {}

    # -- Overpass access ---------------------------------------------------
    def _build_query(self, bbox: str) -> str:
        """One union request, but with a *separate* element budget per category.

        A single shared ``out center N`` truncates in OSM element order, which in
        testing left only 3 attractions out of 120 because hotels and restaurants
        consumed the whole allowance. Per-category sets keep each category
        represented, and it is still a single request against this shared service.
        """
        hotel_filter = "|".join(HOTEL_TOURISM_VALUES)
        food_filter = "|".join(FOOD_AMENITY_VALUES)
        attraction_filter = "|".join(ATTRACTION_TOURISM_VALUES)
        return (
            f"[out:json][timeout:{self.query_timeout_seconds}];"
            f'nwr["tourism"~"^({hotel_filter})$"]({bbox})->.hotels;'
            f'nwr["amenity"~"^({food_filter})$"]({bbox})->.food;'
            f'nwr["tourism"~"^({attraction_filter})$"]({bbox})->.sights;'
            f".hotels out center {self.hotel_limit};"
            f".food out center {self.food_limit};"
            f".sights out center {self.attraction_limit};"
        )

    async def _load_pois(self, destination: str) -> tuple[list[_Poi], SourceReference]:
        """Fetch (or reuse) the POI set for a destination."""
        key = destination.strip().casefold()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1], cached[2]

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Another task may have populated the cache while we waited.
            cached = self._cache.get(key)
            if cached is not None and cached[0] > time.monotonic():
                return cached[1], cached[2]

            place = await self.geo_resolver.resolve(destination)
            self._city_cache[key] = place.city
            query = self._build_query(place.bbox_string)
            payload, endpoint = await self.http.request_json_with_fallback(
                "POST", self.mirrors, data={"data": query}
            )
            self.last_endpoint = endpoint

            observed_at = self._osm_base_timestamp(payload)
            pois = self._parse_elements(payload)
            reference = self._source_reference(
                source_id=f"overpass:{place.bbox_string}",
                observed_at=observed_at,
                request_context={
                    "destination": destination,
                    "resolved_city": place.city,
                    "bbox": place.bbox_string,
                    "endpoint": endpoint,
                    "element_count": len(pois),
                    "attribution": OSM_ATTRIBUTION,
                },
            )
            self._cache[key] = (
                time.monotonic() + self.poi_ttl_seconds,
                pois,
                reference,
            )
            return pois, reference

    @staticmethod
    def _osm_base_timestamp(payload: dict) -> datetime:
        """OSM publishes the data snapshot time; that is the real observation time."""
        raw = ((payload or {}).get("osm3s") or {}).get("timestamp_osm_base")
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_elements(payload: dict) -> list[_Poi]:
        pois: list[_Poi] = []
        seen: set[tuple[str, int]] = set()
        for element in (payload or {}).get("elements") or []:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags") or {}
            if not isinstance(tags, dict) or not tags:
                continue
            center = element.get("center") or {}
            latitude = element.get("lat", center.get("lat"))
            longitude = element.get("lon", center.get("lon"))
            if latitude is None or longitude is None:
                continue
            try:
                element_type = str(element.get("type") or "node")
                element_id = int(element["id"])
                # An element can satisfy two category filters (for example
                # tourism=hotel and amenity=restaurant); emit it once.
                key = (element_type, element_id)
                if key in seen:
                    continue
                seen.add(key)
                pois.append(
                    _Poi(
                        element_type=element_type,
                        element_id=element_id,
                        latitude=float(latitude),
                        longitude=float(longitude),
                        tags=tags,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return pois

    # -- capability methods ------------------------------------------------
    def _city_for(self, destination: str) -> str | None:
        return self._city_cache.get(destination.strip().casefold())

    async def search_hotels(self, profile: TripProfile) -> ProviderResult:
        pois, reference = await self._load_pois(profile.destination)
        city = self._city_for(profile.destination)
        hotels = [
            hotel for poi in pois if (hotel := self._to_hotel(poi, city, reference)) is not None
        ]
        return ProviderResult(
            provider=self.name, data=hotels, source_references=[reference] if hotels else []
        )

    async def search_room_offers(
        self, hotel_ids: list[str], profile: TripProfile
    ) -> ProviderResult:
        # OpenStreetMap carries no rates or availability. Returning an empty
        # success keeps the hotel component explicitly estimated downstream
        # instead of inventing a price.
        return ProviderResult(provider=self.name, data=[], source_references=[])

    async def search_restaurants(self, profile: TripProfile) -> ProviderResult:
        pois, reference = await self._load_pois(profile.destination)
        city = self._city_for(profile.destination)
        restaurants = [
            restaurant
            for poi in pois
            if (restaurant := self._to_restaurant(poi, city, reference)) is not None
        ]
        return ProviderResult(
            provider=self.name,
            data=restaurants,
            source_references=[reference] if restaurants else [],
        )

    async def search_attractions(self, profile: TripProfile) -> ProviderResult:
        pois, reference = await self._load_pois(profile.destination)
        city = self._city_for(profile.destination)
        attractions = [
            attraction
            for poi in pois
            if (attraction := self._to_attraction(poi, city, reference)) is not None
        ]
        return ProviderResult(
            provider=self.name,
            data=attractions,
            source_references=[reference] if attractions else [],
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            status="healthy",
            latency_ms=0.0,
            detail=f"OpenStreetMap via {self.last_endpoint or self.mirrors[0]}",
        )

    # -- mapping -----------------------------------------------------------
    def _to_hotel(self, poi: _Poi, city: str | None, reference: SourceReference) -> Hotel | None:
        tags = poi.tags
        if _clean(tags.get("tourism")) not in HOTEL_TOURISM_VALUES:
            return None
        name = _pick_name(tags)
        if not name:
            return None
        # OSM has no guest review score. ``stars`` is an official hotel
        # classification, so it is recorded on the source reference rather than
        # being written into ``rating`` (where it would silently be scored as a
        # guest rating). See _parse_star_classification.
        star_classification = _parse_star_classification(tags.get("stars"))
        return Hotel(
            id=osm_stable_id(poi.element_type, poi.element_id),
            canonical_name=name,
            aliases=_collect_aliases(tags, name),
            location=self._location(poi, tags, city, reference),
            rating=None,
            review_count=None,
            brand=_clean(tags.get("brand")),
            source_references=[
                self._entity_reference(
                    poi,
                    reference,
                    extra_context=(
                        {"star_classification": star_classification}
                        if star_classification is not None
                        else None
                    ),
                )
            ],
        )

    def _to_restaurant(
        self, poi: _Poi, city: str | None, reference: SourceReference
    ) -> Restaurant | None:
        tags = poi.tags
        if _clean(tags.get("amenity")) not in FOOD_AMENITY_VALUES:
            return None
        name = _pick_name(tags)
        if not name:
            return None
        cuisine_raw = _clean(tags.get("cuisine"))
        cuisine = (
            [part.strip() for part in cuisine_raw.split(";") if part.strip()] if cuisine_raw else []
        )
        charge = _parse_decimal(tags.get("charge"))
        return Restaurant(
            id=osm_stable_id(poi.element_type, poi.element_id),
            canonical_name=name,
            aliases=_collect_aliases(tags, name),
            location=self._location(poi, tags, city, reference),
            rating=None,
            review_count=None,
            average_cost=(
                Money(
                    value=charge,
                    currency="CNY",
                    price_type=PriceType.PER_PERSON,
                    source_reference_id=reference.source_id,
                )
                if charge is not None
                else None
            ),
            cuisine=cuisine,
            source_references=[self._entity_reference(poi, reference)],
        )

    def _to_attraction(
        self, poi: _Poi, city: str | None, reference: SourceReference
    ) -> Attraction | None:
        tags = poi.tags
        tourism = _clean(tags.get("tourism"))
        if tourism not in ATTRACTION_TOURISM_VALUES:
            return None
        name = _pick_name(tags)
        if not name:
            return None
        charge = _parse_decimal(tags.get("charge") or tags.get("fee"))
        return Attraction(
            id=osm_stable_id(poi.element_type, poi.element_id),
            canonical_name=name,
            aliases=_collect_aliases(tags, name),
            location=self._location(poi, tags, city, reference),
            rating=None,
            ticket_price=(
                Money(
                    value=charge,
                    currency="CNY",
                    price_type=PriceType.PER_PERSON,
                    source_reference_id=reference.source_id,
                )
                if charge is not None
                else None
            ),
            recommended_duration_minutes=DEFAULT_DURATION_BY_TYPE.get(tourism, 90),
            opening_hours=parse_opening_hours(tags.get("opening_hours")),
            source_references=[
                self._entity_reference(poi, reference, extra_context=_wiki_link_context(tags))
            ],
        )

    @staticmethod
    def _location(poi: _Poi, tags: dict, city: str | None, reference: SourceReference) -> Location:
        street = _clean(tags.get("addr:street"))
        number = _clean(tags.get("addr:housenumber"))
        suburb = _clean(tags.get("addr:suburb") or tags.get("addr:district"))
        address_parts = [part for part in (suburb, street, number) if part]
        address = (
            " ".join(address_parts) if address_parts else (_clean(tags.get("addr:full")) or "")
        )
        if not address:
            address = f"{poi.latitude:.5f}, {poi.longitude:.5f}"
        return Location(
            address=address,
            city=_clean(tags.get("addr:city")) or city or "",
            district=suburb,
            coordinates=Coordinates(latitude=poi.latitude, longitude=poi.longitude),
            source_reference_id=reference.source_id,
        )

    def _entity_reference(
        self,
        poi: _Poi,
        base: SourceReference,
        *,
        extra_context: dict | None = None,
    ) -> SourceReference:
        return base.model_copy(
            update={
                "source_id": f"osm:{poi.identity}",
                "request_context": {
                    **base.request_context,
                    "osm_type": poi.element_type,
                    "osm_id": poi.element_id,
                    **(extra_context or {}),
                },
            }
        )

    def _source_reference(
        self, *, source_id: str, observed_at: datetime, request_context: dict
    ) -> SourceReference:
        return SourceReference(
            source=self.name,
            source_type=SourceType.OPEN_DATA,
            source_id=source_id,
            observed_at=observed_at,
            fetched_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.poi_ttl_seconds),
            request_context=request_context,
            data_version="osm-live",
            confidence=0.9,
            domain=DataDomain.GEO_FACT,
        )
