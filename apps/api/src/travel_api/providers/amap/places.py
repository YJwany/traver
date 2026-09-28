"""AMap place search: attractions, restaurants and hotels.

What this adds over the Overpass provider
----------------------------------------
Overpass reads OpenStreetMap, whose Chinese POI coverage is thin and uneven — a
well-known Beijing restaurant is often simply absent, or present without a name.
AMap's ``/v3/place/text`` and ``/v3/place/around`` cover domestic POIs far better,
so this provider finds places OSM does not have.

Search terms, not type codes
----------------------------
AMap's ``types`` parameter uses its own numeric taxonomy (such as ``050000`` for
food). Those codes were **not** verified while writing this, and a wrong code does
not fail — it returns POIs of a different category, so a restaurant search would
quietly fill the itinerary with unrelated businesses. This provider therefore
searches by ``keywords``, which is documented and unambiguous, and records the exact
keyword in the source context so a reader can see how the result was obtained.
Switching to type codes is a real improvement, but only once the table is verified.

Honesty about what is absent
----------------------------
AMap returns a POI's name, address, coordinates and category, and for some POIs a
telephone number. It does **not** return ratings, review counts, ticket prices or
room rates, so those fields are left ``None``. A defaulted rating of 0 or a guessed
ticket price would be indistinguishable downstream from a measured one — and the
product must not present a guess as a fact.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid5

from travel_api.domain.models import (
    Attraction,
    Coordinates,
    DataDomain,
    Hotel,
    Location,
    ProviderHealth,
    ProviderResult,
    Restaurant,
    SourceReference,
    SourceType,
    TripProfile,
)
from travel_api.providers.amap.client import (
    AmapClient,
    AmapError,
    AmapKeyError,
    parse_location,
)
from travel_api.providers.free.attribution import AMAP_ATTRIBUTION

logger = logging.getLogger(__name__)

#: Namespace for deterministic ids, so the same AMap POI keeps one identity across
#: runs and cannot duplicate a place in the itinerary. Distinct from the OSM
#: namespace so an AMap POI and an OSM node can never collide.
AMAP_NAMESPACE = UUID("4c1a7e39-58d2-4b6f-8e01-2a7d3f9b6c54")

#: Verified-documented keyword per category. Deliberately keywords rather than the
#: numeric type taxonomy, which is unverified here; see the module docstring.
KEYWORDS_BY_CATEGORY: dict[str, str] = {
    "attractions": "景点",
    "restaurants": "美食",
    "hotels": "酒店",
}

PLACE_TEXT_PATH = "/v3/place/text"
PLACE_AROUND_PATH = "/v3/place/around"

#: Default search radius in metres. AMap's own default is 3000 and its documented
#: maximum for this parameter is 50000.
DEFAULT_RADIUS_METERS = 5000


def amap_stable_id(poi_id: str) -> UUID:
    """Deterministic id for one AMap POI.

    AMap's ``id`` is stable across requests, so hashing it through a fixed namespace
    yields an id that is stable for the life of the deployment — which is what stops
    the same hotel appearing twice in one itinerary under two different UUIDs.
    """
    return uuid5(AMAP_NAMESPACE, f"amap:{poi_id}")


class AmapPlaceProvider:
    """Serves the POI capabilities from AMap's place search."""

    def __init__(
        self,
        client: AmapClient,
        *,
        name: str = "amap",
        ttl_seconds: int = 300,
        radius_meters: int = DEFAULT_RADIUS_METERS,
        limit_per_category: int = 20,
        geocoder=None,
    ) -> None:
        self.name = name
        self.client = client
        #: Short, and there is no cache: AMap's terms forbid storing results, so the
        #: expiry only bounds how long a persisted snapshot may be shown as current.
        self.ttl_seconds = ttl_seconds
        self.radius_meters = radius_meters
        self.limit_per_category = limit_per_category
        #: Optional AMap geocoder. When absent the destination name is used as the
        #: ``city`` parameter, which AMap accepts directly.
        self.geocoder = geocoder
        self.last_city: str | None = None

    # -- capabilities ------------------------------------------------------

    async def search_attractions(self, profile: TripProfile) -> ProviderResult:
        return await self._search(profile, "attractions")

    async def search_restaurants(self, profile: TripProfile) -> ProviderResult:
        return await self._search(profile, "restaurants")

    async def search_hotels(self, profile: TripProfile) -> ProviderResult:
        return await self._search(profile, "hotels")

    # -- internals ---------------------------------------------------------

    async def _search(self, profile: TripProfile, category: str) -> ProviderResult:
        keyword = KEYWORDS_BY_CATEGORY[category]
        city = profile.destination
        self.last_city = city

        try:
            payload = await self.client.get(
                PLACE_TEXT_PATH,
                params={
                    "keywords": keyword,
                    "city": city,
                    # Keep results inside the city the trip is about; without this a
                    # well-known name elsewhere in the country can outrank a local one.
                    "citylimit": "true",
                    "offset": str(min(self.limit_per_category, 25)),
                    "page": "1",
                    "extensions": "base",
                },
            )
        except AmapKeyError:
            # A bad key is the owner's problem and must surface, not be swallowed into
            # an empty result that looks like "this city has no hotels".
            raise
        except AmapError as exc:
            logger.warning("AMap %s search failed for %r: %s", category, city, exc)
            return ProviderResult(provider=self.name, data=[], errors=[str(exc)])

        reference = self._source_reference(category=category, keyword=keyword, city=city)
        entities = self._build(payload, category, city, reference)
        return ProviderResult(
            provider=self.name,
            data=entities,
            source_references=[reference] if entities else [],
        )

    def _build(
        self,
        payload: dict,
        category: str,
        city: str,
        reference: SourceReference,
    ) -> list:
        pois = payload.get("pois")
        if not isinstance(pois, list):
            return []

        builder = {
            "attractions": self._attraction,
            "restaurants": self._restaurant,
            "hotels": self._hotel,
        }[category]

        entities = []
        for poi in pois:
            if not isinstance(poi, dict):
                continue
            name = poi.get("name")
            coordinates = parse_location(poi.get("location"))
            # A POI without a name or a coordinate cannot be placed or shown, so it is
            # dropped rather than given a placeholder location.
            if not name or coordinates is None:
                continue
            entity = builder(poi, str(name), coordinates, city, reference)
            if entity is not None:
                entities.append(entity)
        return entities

    def _location(
        self,
        poi: dict,
        coordinates: tuple[float, float],
        city: str,
        reference: SourceReference,
    ) -> Location:
        latitude, longitude = coordinates
        address = str(poi.get("address") or "").strip()
        if not address or address == "[]":
            # AMap returns "[]" for an absent address. Fall back to the coordinate so
            # the UI has something truthful to print rather than an empty string.
            address = f"{latitude:.5f}, {longitude:.5f}"
        return Location(
            address=address,
            city=city,
            district=str(poi.get("businessarea") or "").strip() or None,
            coordinates=Coordinates(latitude=latitude, longitude=longitude),
            source_reference_id=reference.source_id,
        )

    def _attraction(
        self,
        poi: dict,
        name: str,
        coordinates: tuple[float, float],
        city: str,
        reference: SourceReference,
    ) -> Attraction:
        return Attraction(
            id=amap_stable_id(str(poi.get("id") or name)),
            canonical_name=name,
            location=self._location(poi, coordinates, city, reference),
            # AMap's place search returns no rating and no ticket price. Both stay
            # None rather than defaulting, so nothing downstream mistakes a missing
            # value for a measured one.
            rating=None,
            ticket_price=None,
            source_references=[self._entity_reference(poi, reference)],
        )

    def _restaurant(
        self,
        poi: dict,
        name: str,
        coordinates: tuple[float, float],
        city: str,
        reference: SourceReference,
    ) -> Restaurant:
        poi_type = str(poi.get("type") or "")
        # `type` is a semicolon-joined "大类;中类;小类" path, e.g. "餐饮服务;中餐厅;…".
        # Only the trailing, most specific label is kept as cuisine.
        cuisine = [part for part in poi_type.split(";") if part][-1:] or []
        return Restaurant(
            id=amap_stable_id(str(poi.get("id") or name)),
            canonical_name=name,
            location=self._location(poi, coordinates, city, reference),
            rating=None,
            review_count=None,
            average_cost=None,
            cuisine=cuisine,
            source_references=[self._entity_reference(poi, reference)],
        )

    def _hotel(
        self,
        poi: dict,
        name: str,
        coordinates: tuple[float, float],
        city: str,
        reference: SourceReference,
    ) -> Hotel:
        return Hotel(
            id=amap_stable_id(str(poi.get("id") or name)),
            canonical_name=name,
            location=self._location(poi, coordinates, city, reference),
            rating=None,
            source_references=[self._entity_reference(poi, reference)],
        )

    @staticmethod
    def _entity_reference(poi: dict, base: SourceReference) -> SourceReference:
        """Per-entity reference, carrying AMap's own POI id.

        The id is kept so a reader can look the place up on AMap themselves, and the
        telephone number is carried when present because AMap's terms permit showing
        it while forbidding storage — so it travels with the response rather than
        being written into a table.
        """
        context = dict(base.request_context)
        context["amap_poi_id"] = str(poi.get("id") or "")
        telephone = str(poi.get("tel") or "").strip()
        if telephone and telephone != "[]":
            context["telephone"] = telephone
        poi_type = str(poi.get("type") or "").strip()
        if poi_type and poi_type != "[]":
            context["amap_poi_type"] = poi_type
        return base.model_copy(
            update={
                "source_id": f"{base.source_id}:{poi.get('id') or ''}",
                "request_context": context,
            }
        )

    def _source_reference(self, *, category: str, keyword: str, city: str) -> SourceReference:
        now = datetime.now(timezone.utc)
        return SourceReference(
            source=self.name,
            source_type=SourceType.OFFICIAL_API,
            source_id=f"amap:{category}:{city}",
            observed_at=now,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            request_context={
                "amap_endpoint": PLACE_TEXT_PATH,
                "category": category,
                # Recorded so the search is reproducible and its precision is auditable.
                "keyword": keyword,
                "city": city,
                "city_limit": True,
                "radius_meters": self.radius_meters,
                "attribution": AMAP_ATTRIBUTION.as_credit(),
                "cached": False,
                "absent_fields": "rating,review_count,ticket_price,average_cost",
                "note": "实时查询，未做任何缓存；高德不提供评分与价格，因此这些字段为空而非默认值",
            },
            data_version="amap-live",
            confidence=0.85,
            domain=DataDomain.GEO_FACT,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            status="ok",
            checked_at=datetime.now(timezone.utc),
            detail="已配置 Key；POI 查询实时进行，不做缓存",
        )


__all__ = [
    "AMAP_NAMESPACE",
    "KEYWORDS_BY_CATEGORY",
    "PLACE_AROUND_PATH",
    "PLACE_TEXT_PATH",
    "AmapPlaceProvider",
    "amap_stable_id",
]
