from __future__ import annotations

import json
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from travel_api.domain.models import (
    Attraction,
    Coordinates,
    DataDomain,
    Hotel,
    HotelOffer,
    Location,
    Money,
    OpeningHours,
    PriceType,
    ProviderHealth,
    ProviderResult,
    Restaurant,
    RouteLeg,
    RoutePoint,
    SourceReference,
    TravelMode,
    TripProfile,
)
from travel_api.providers.mock import source_reference, stable_id


class FixtureLoader:
    """Loads deterministic provider records without coupling domain code to JSON shape."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def records(self, name: str) -> list[dict[str, Any]]:
        path = self.root / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"fixture not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError(f"fixture must be a JSON array of objects: {path}")
        return payload


class FixtureTravelProvider:
    """TravelProvider backed by versioned JSON fixtures.

    The loader intentionally maps source records into the same domain models
    as a future official provider. Aggregation and decision services therefore
    do not need a fixture-specific branch.
    """

    def __init__(self, name: str, fixture_root: str | Path) -> None:
        self.name = name
        self.loader = FixtureLoader(fixture_root)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            status="healthy",
            latency_ms=1.0,
            detail=f"fixture provider: {self.loader.root}",
        )

    async def search_hotels(self, profile: TripProfile) -> ProviderResult:
        hotels = [self._hotel(row) for row in self.loader.records("hotels")]
        refs = [ref for item in hotels for ref in item.source_references]
        return ProviderResult(provider=self.name, data=hotels, source_references=refs)

    async def search_room_offers(
        self, hotel_ids: list[str], profile: TripProfile
    ) -> ProviderResult:
        requested = {str(value) for value in hotel_ids}
        hotels_by_source = {
            row["source_id"]: self._hotel_id(row) for row in self.loader.records("hotels")
        }
        offers = []
        for row in self.loader.records("hotel_offers"):
            hotel_id = hotels_by_source.get(row["hotel_source_id"])
            if hotel_id is None or str(hotel_id) not in requested:
                continue
            reference = self._reference(row, DataDomain.COMMERCIAL_FACT, ttl_minutes=5)
            offers.append(
                HotelOffer(
                    id=stable_id(f"fixture:offer:{row['source']}:{row['source_id']}"),
                    hotel_id=hotel_id,
                    room_type_id=stable_id(f"fixture:room:{row['source']}:{row['room_type']}"),
                    rate_plan_id=stable_id(f"fixture:rate:{row['source']}:{row['rate_plan']}"),
                    check_in=profile.start_date,
                    check_out=profile.end_date,
                    guests=profile.travelers,
                    room_count=1,
                    meal_plan=row.get("meal_plan"),
                    cancellation_policy=row.get("cancellation_policy"),
                    payment_policy=row.get("payment_policy"),
                    price=Money(
                        value=Decimal(str(row["price"])),
                        currency=row.get("currency", "CNY"),
                        price_type=PriceType(row.get("price_type", "per_stay")),
                        source_reference_id=reference.source_id,
                    ),
                    availability=row.get("availability"),
                    source_reference=reference,
                )
            )
        return ProviderResult(
            provider=self.name,
            data=offers,
            source_references=[item.source_reference for item in offers],
        )

    async def search_restaurants(self, profile: TripProfile) -> ProviderResult:
        restaurants = [self._restaurant(row) for row in self.loader.records("restaurants")]
        refs = [ref for item in restaurants for ref in item.source_references]
        return ProviderResult(provider=self.name, data=restaurants, source_references=refs)

    async def search_attractions(self, profile: TripProfile) -> ProviderResult:
        attractions = [self._attraction(row) for row in self.loader.records("attractions")]
        refs = [ref for item in attractions for ref in item.source_references]
        return ProviderResult(provider=self.name, data=attractions, source_references=refs)

    async def calculate_routes(
        self, profile: TripProfile, points: list[RoutePoint]
    ) -> ProviderResult:
        hotels = {row["name"]: self._hotel_id(row) for row in self.loader.records("hotels")}
        attractions = {
            row["name"]: self._attraction_id(row) for row in self.loader.records("attractions")
        }
        routes = []
        for row in self.loader.records("routes"):
            origin_id = hotels.get(row["origin"], stable_id(f"fixture:place:{row['origin']}"))
            destination_id = attractions.get(
                row["destination"], stable_id(f"fixture:place:{row['destination']}")
            )
            reference = self._reference(row, DataDomain.GEO_FACT, ttl_minutes=10)
            routes.append(
                RouteLeg(
                    origin_id=origin_id,
                    destination_id=destination_id,
                    mode=TravelMode(row.get("mode", "transit")),
                    distance_meters=int(row["distance_meters"]),
                    duration_minutes=int(row["duration_minutes"]),
                    cost=Money(
                        value=Decimal(str(row.get("cost", 0))),
                        currency=row.get("currency", "CNY"),
                        price_type=PriceType.PER_PERSON,
                        source_reference_id=reference.source_id,
                    ),
                    source_reference=reference,
                )
            )
        return ProviderResult(
            provider=self.name,
            data=routes,
            source_references=[item.source_reference for item in routes],
        )

    def _hotel(self, row: dict[str, Any]) -> Hotel:
        reference = self._reference(row, DataDomain.COMMERCIAL_FACT)
        return Hotel(
            id=self._hotel_id(row),
            canonical_name=row["name"],
            aliases=row.get("aliases", []),
            location=self._location(row),
            rating=row.get("rating"),
            review_count=row.get("review_count"),
            source_references=[reference],
        )

    def _restaurant(self, row: dict[str, Any]) -> Restaurant:
        reference = self._reference(row, DataDomain.COMMERCIAL_FACT)
        return Restaurant(
            id=stable_id(f"fixture:restaurant:{row['source']}:{row['source_id']}"),
            canonical_name=row["name"],
            location=self._location(row),
            rating=row.get("rating"),
            average_cost=Money(
                value=Decimal(str(row["average_cost"])),
                currency=row.get("currency", "CNY"),
                price_type=PriceType.PER_PERSON,
                source_reference_id=reference.source_id,
            )
            if row.get("average_cost") is not None
            else None,
            cuisine=row.get("cuisine", []),
            source_references=[reference],
        )

    def _attraction(self, row: dict[str, Any]) -> Attraction:
        reference = self._reference(row, DataDomain.GEO_FACT, ttl_minutes=60)
        raw_hours = row.get("opening_hours", {})
        if isinstance(raw_hours, dict):
            hours = [
                OpeningHours(
                    weekday=weekday,
                    opens_at=time.fromisoformat(raw_hours["opens_at"]),
                    closes_at=time.fromisoformat(raw_hours["closes_at"]),
                )
                for weekday in range(7)
            ]
        else:
            hours = [OpeningHours.model_validate(item) for item in raw_hours]
        return Attraction(
            id=self._attraction_id(row),
            canonical_name=row["name"],
            location=self._location(row),
            rating=row.get("rating"),
            ticket_price=Money(
                value=Decimal(str(row.get("ticket_price", 0))),
                currency=row.get("currency", "CNY"),
                price_type=PriceType.PER_PERSON,
                source_reference_id=reference.source_id,
            ),
            recommended_duration_minutes=int(row.get("recommended_duration_minutes", 120)),
            opening_hours=hours,
            source_references=[reference],
        )

    def _reference(
        self, row: dict[str, Any], domain: DataDomain, ttl_minutes: int = 30
    ) -> SourceReference:
        return source_reference(
            str(row.get("source", self.name)), str(row["source_id"]), domain, ttl_minutes
        )

    @staticmethod
    def _location(row: dict[str, Any]) -> Location:
        return Location(
            address=row["address"],
            city=row.get("city", ""),
            coordinates=Coordinates(
                latitude=float(row["latitude"]), longitude=float(row["longitude"])
            ),
        )

    @staticmethod
    def _hotel_id(row: dict[str, Any]):
        return stable_id(f"fixture:hotel:{row['source']}:{row['source_id']}")

    @staticmethod
    def _attraction_id(row: dict[str, Any]):
        return stable_id(f"fixture:attraction:{row['source']}:{row['source_id']}")
