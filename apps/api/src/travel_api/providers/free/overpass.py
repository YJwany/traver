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
    ElementAge,
    ElementAgeKind,
    Hotel,
    Location,
    Money,
    ObservationBasis,
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
from travel_api.services.freshness import FreshnessService

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
    #: When this element was last edited in OSM, from ``out ... meta``.
    #:
    #: This is the age of the *fact*, and it is a different number from the age of the response.
    #: Measured against the live service, the OSM database snapshot is typically minutes old
    #: while the median element in a well-mapped city was last touched **years** ago — a
    #: restaurant that closed in 2019 is still served from a snapshot taken this minute. See
    #: ``element_age`` on the reference, and ``docs/architecture/realtime-data.md``.
    #:
    #: ``None`` when the response carried no metadata, which must not be read as "just edited".
    edited_at: datetime | None = None

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

        ``meta`` is requested so each element carries its own last-edit ``timestamp``. Without
        it the only age available is ``osm3s.timestamp_osm_base`` — the OSM database snapshot,
        which is minutes old for a city whose elements are years old. The two are reported
        separately rather than conflated; see ``_source_reference``.
        """
        hotel_filter = "|".join(HOTEL_TOURISM_VALUES)
        food_filter = "|".join(FOOD_AMENITY_VALUES)
        attraction_filter = "|".join(ATTRACTION_TOURISM_VALUES)
        # ``center`` before ``meta``: verbosity/geometry ordering is documented either way
        # round in practice, but this is the form verified against the live service, including
        # that ways still come back with a ``center``. Losing it would drop every way.
        return (
            f"[out:json][timeout:{self.query_timeout_seconds}];"
            f'nwr["tourism"~"^({hotel_filter})$"]({bbox})->.hotels;'
            f'nwr["amenity"~"^({food_filter})$"]({bbox})->.food;'
            f'nwr["tourism"~"^({attraction_filter})$"]({bbox})->.sights;'
            f".hotels out center meta {self.hotel_limit};"
            f".food out center meta {self.food_limit};"
            f".sights out center meta {self.attraction_limit};"
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

            osm_base = self._osm_base_timestamp(payload)
            pois = self._parse_elements(payload)
            reference = self._source_reference(
                source_id=f"overpass:{place.bbox_string}",
                osm_base=osm_base,
                pois=pois,
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
    def _osm_base_timestamp(payload: dict) -> datetime | None:
        """OSM's published snapshot time, or ``None`` if it did not publish one.

        Returning ``None`` rather than a substituted "now" is the whole point. The caller
        labels the reference ``PROVIDER_REPORTED`` only when this returns a real value, and
        ``PROVIDER_REPORTED`` is what makes ``FreshnessService`` treat the age as a fact about
        the *data*. Falling back to the current time while still claiming the source reported
        it would produce a reference that reads as "this OSM snapshot is 0 seconds old, and OSM
        said so" — when OSM said nothing. That is a fabricated observation time on the very
        field the 时效性 rule exists to protect.
        """
        raw = ((payload or {}).get("osm3s") or {}).get("timestamp_osm_base")
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        return None

    @staticmethod
    def _element_timestamp(element: dict) -> datetime | None:
        """One element's last-edit time, from the ``meta`` block.

        Separate from ``_osm_base_timestamp`` because they answer different questions and have
        been confused for each other: this one is when *this fact* last changed, that one is
        when the database we asked was last rebuilt. Returns ``None`` when absent — an element
        with no metadata is not an element that was just edited.
        """
        raw = element.get("timestamp")
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

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
                        edited_at=OverpassPoiProvider._element_timestamp(element),
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

    #: Declared so the capability matrix reports room offers as uncovered rather than as
    #: fully served by this source. The method below must exist — one provider serves
    #: hotels, restaurants and attractions together — but returning an empty list is the
    #: whole of its behaviour, and "returns nothing" is not coverage. Without this, the
    #: settings page advertised room rates as available in a deployment where no source
    #: can produce one.
    UNSERVED_CAPABILITIES = frozenset({"search_room_offers"})

    async def search_room_offers(self, hotels: list[Hotel], profile: TripProfile) -> ProviderResult:
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

    @staticmethod
    def _element_age(pois: list[_Poi], *, now: datetime) -> ElementAge | None:
        """The age distribution of the elements just returned, or ``None`` if nobody said.

        The statistic itself lives on :meth:`ElementAge.of`, shared with the review path, so the
        two cannot drift into reporting the same shape two different ways. The horizon comes from
        the freshness policy, so the count of over-age elements is measured against the same rule
        that will report them — one policy, not two.
        """
        return ElementAge.of(
            (poi.edited_at for poi in pois),
            now=now,
            horizon_seconds=FreshnessService().policy.max_age_for("attraction"),
            member_kind=ElementAgeKind.EDITED,
        )

    def _source_reference(
        self, *, source_id: str, osm_base: datetime | None, request_context: dict, pois: list[_Poi]
    ) -> SourceReference:
        """One reference for this POI set, with the basis the response actually supports.

        Two outcomes, and they must not be merged:

        * OSM published ``timestamp_osm_base`` — the age is a fact about the data, so the basis
          is ``PROVIDER_REPORTED`` and ``observed_at`` is that instant;
        * OSM published nothing usable — we still need an ``observed_at``, so it becomes our
          fetch time, but the basis is ``SOURCE_SILENT`` and the absence is recorded in the
          request context. ``SOURCE_SILENT`` is what tells every downstream reader that the
          timestamp describes *our request*, not the data.

        This mirrors ``amap/weather.py``, which handles the same fork for 高德's ``reporttime``.
        Before this, the missing case wrote the current time and still claimed
        ``PROVIDER_REPORTED``, so an undated OSM response produced a reference asserting that
        the data was seconds old and that OSM had said so.

        A third thing is attached that neither of the two cases expresses: ``element_age``.
        ``observed_at`` here is the snapshot instant, and it is genuinely reported — but it
        describes *our copy of the database*, not the facts in it. A snapshot cut a minute ago
        routinely contains elements last edited years earlier, so on its own it would report
        those facts as a minute old. The distribution goes alongside rather than replacing it,
        because both statements are true and a reader needs both.
        """
        now = datetime.now(timezone.utc)
        if osm_base is None:
            context = {**request_context, "osm_base_missing": True}
            observed_at = now
            basis = ObservationBasis.SOURCE_SILENT
        else:
            context = request_context
            observed_at = osm_base
            basis = ObservationBasis.PROVIDER_REPORTED
        element_age = self._element_age(pois, now=now)
        if element_age is not None:
            # Recorded in the context as well, so a reader with only the JSON has it without
            # knowing about the typed field.
            context = {
                **context,
                "element_age_median_days": element_age.median_days,
                "element_age_oldest_days": round(element_age.oldest_seconds / 86400, 1),
                "element_age_over_horizon": element_age.older_than_horizon,
            }
        return SourceReference(
            source=self.name,
            source_type=SourceType.OPEN_DATA,
            source_id=source_id,
            observed_at=observed_at,
            observed_at_basis=basis,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.poi_ttl_seconds),
            element_age=element_age,
            request_context=context,
            data_version="osm-live",
            confidence=0.9,
            domain=DataDomain.GEO_FACT,
        )
